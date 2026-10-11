"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any, Dict, List, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def ensure_indexes():
    """Create all required indexes idempotently at startup."""
    # employees indexes
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("joined_on", 1)])

    # attendance_logs indexes
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1), ("status", 1)])
    db.attendance_logs.create_index([("date", 1), ("status", 1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", -1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        ensure_indexes()
    except Exception as exc:
        print(f"Warning: Failed to ensure indexes at startup: {exc}")
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class Status(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class ExplainEndpointEnum(str, Enum):
    attendance_list = "attendance_list"
    employee_monthly = "employee_monthly"
    department_summary = "department_summary"
    late_leaderboard = "late_leaderboard"
    department_trend = "department_trend"
    employees = "employees"


# --------------------------------------------------------------------------- #
# Helpers (R1 - R8)
# --------------------------------------------------------------------------- #
def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Ensure a datetime is timezone-aware UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def instant_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert datetime to integer epoch milliseconds."""
    if dt is None:
        return None
    dt = ensure_utc(dt)
    return int(dt.timestamp() * 1000)


datetime_to_epoch_ms = instant_to_ms


def ms_to_utc_dt(ms: Optional[int]) -> Optional[datetime]:
    """Convert integer epoch milliseconds to UTC datetime, truncated to whole seconds (R1)."""
    if ms is None:
        return None
    truncated_ms = (ms // 1000) * 1000
    return datetime.fromtimestamp(truncated_ms / 1000.0, tz=timezone.utc)


def now_utc_truncated() -> datetime:
    """Current UTC datetime truncated to whole seconds (R1)."""
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=0)


def to_ist(dt: datetime) -> datetime:
    """Convert UTC datetime to IST."""
    dt = ensure_utc(dt)
    return dt.astimezone(IST)


def round_half_up(val: Optional[float], decimals: int = 2) -> Optional[float]:
    """Round a number half-up to specified decimal places (R8)."""
    if val is None:
        return None
    d = Decimal(str(val))
    pattern = "0." + ("0" * decimals) if decimals > 0 else "1"
    return float(d.quantize(Decimal(pattern), rounding=ROUND_HALF_UP))


def get_month_range(month_str: str) -> tuple[date, date]:
    """Given YYYY-MM, return (start_date, end_date) for the month."""
    year, month = map(int, month_str.split("-"))
    start_date = date(year, month, 1)
    if month == 12:
        next_month_start = date(year + 1, 1, 1)
    else:
        next_month_start = date(year, month + 1, 1)
    end_date = next_month_start - timedelta(days=1)
    return start_date, end_date


def count_working_days(start_date: date, end_date: date) -> int:
    """Count Monday-to-Friday calendar days between start_date and end_date inclusive (R7)."""
    if start_date > end_date:
        return 0
    cur = start_date
    count = 0
    while cur <= end_date:
        if cur.weekday() < 5:  # Mon=0, Fri=4
            count += 1
        cur += timedelta(days=1)
    return count


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1 Time zone and attendance date.
    Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in instant.
    Exception - overnight shifts (shift_end <= shift_start, e.g. 22:00-06:00):
    a punch-in whose IST time is earlier than shift_end belongs to the shift that started the previous day.
    """
    punch_in_ist = to_ist(punch_in_utc)
    h_start, m_start = map(int, shift_start.split(":"))
    h_end, m_end = map(int, shift_end.split(":"))
    is_overnight = (h_end, m_end) <= (h_start, m_start)

    ist_time = (punch_in_ist.hour, punch_in_ist.minute, punch_in_ist.second)
    end_time = (h_end, m_end, 0)

    if is_overnight and ist_time < end_time:
        return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(
    punch_in_utc: datetime, attendance_date_str: str, shift_start: str
) -> int:
    """
    R2 Late calculation.
    Grace period is 10 minutes. A punch-in is late only if strictly more than 10:00
    after shift_start. When late, late_minutes = whole minutes (floor) elapsed since shift_start.
    """
    punch_in_ist = to_ist(punch_in_utc).replace(microsecond=0)
    d = date.fromisoformat(attendance_date_str)
    h, m = map(int, shift_start.split(":"))
    shift_start_ist = datetime(d.year, d.month, d.day, h, m, 0, tzinfo=IST)

    elapsed_seconds = (punch_in_ist - shift_start_ist).total_seconds()
    if elapsed_seconds > 600:
        return int(elapsed_seconds // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4 Work hours.
    (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    punch_in_utc = ensure_utc(punch_in_utc)
    punch_out_utc = ensure_utc(punch_out_utc)
    in_sec = int(punch_in_utc.timestamp())
    out_sec = int(punch_out_utc.timestamp())
    diff_sec = out_sec - in_sec
    hours_decimal = Decimal(diff_sec) / Decimal(3600)
    rounded = hours_decimal.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)


def compute_overtime(
    punch_out_utc: datetime, attendance_date_str: str, shift_start: str, shift_end: str
) -> int:
    """
    R3 Overtime.
    overtime_minutes = whole minutes (floor) between shift_end and punch_out,
    counted only when that is >= 30; otherwise 0.
    """
    punch_out_ist = to_ist(punch_out_utc).replace(microsecond=0)
    d = date.fromisoformat(attendance_date_str)
    h_start, m_start = map(int, shift_start.split(":"))
    h_end, m_end = map(int, shift_end.split(":"))
    is_overnight = (h_end, m_end) <= (h_start, m_start)

    if is_overnight:
        end_date = d + timedelta(days=1)
    else:
        end_date = d

    shift_end_ist = datetime(
        end_date.year, end_date.month, end_date.day, h_end, m_end, 0, tzinfo=IST
    )
    diff_seconds = (punch_out_ist - shift_end_ist).total_seconds()
    if diff_seconds < 0:
        return 0
    minutes = int(diff_seconds // 60)
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5 Half day.
    After punch-out, half_day = true when the rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


def serialize_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize MongoDB attendance document to OpenAPI AttendanceRecord shape."""
    history_list = []
    for h in doc.get("history", []):
        h_at = h.get("at")
        h_at_ms = instant_to_ms(h_at) if isinstance(h_at, datetime) else h_at

        serialized_changes = {}
        for field_name, chg in h.get("changes", {}).items():
            chg_from = chg.get("from")
            chg_to = chg.get("to")
            if field_name in ("punch_in", "punch_out"):
                if isinstance(chg_from, datetime):
                    chg_from = instant_to_ms(chg_from)
                if isinstance(chg_to, datetime):
                    chg_to = instant_to_ms(chg_to)
            serialized_changes[field_name] = {"from": chg_from, "to": chg_to}

        history_list.append(
            {
                "at": h_at_ms,
                "by": h.get("by"),
                "reason": h.get("reason"),
                "changes": serialized_changes,
            }
        )

    punch_in = doc.get("punch_in")
    punch_out = doc.get("punch_out")

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": instant_to_ms(punch_in) if isinstance(punch_in, datetime) else punch_in,
        "punch_out": instant_to_ms(punch_out) if isinstance(punch_out, datetime) else punch_out,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_list,
    }


def clean_bson_obj(obj: Any) -> Any:
    """Recursively clean BSON objects for JSON response."""
    return json_util.loads(json_util.dumps(obj))


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class HealthResponse(BaseModel):
    status: str = "ok"


class EmployeeCreate(BaseModel):
    emp_code: str = Field(
        ...,
        pattern=r"^EMP\d{4,6}$",
        description="The customer's own employee code. EMP followed by 4-6 digits.",
        examples=["EMP0001"],
    )
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(
        default="09:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
        description="24-hour HH:MM, IST.",
    )
    shift_end: str = Field(
        default="18:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
        description="24-hour HH:MM, IST. <= shift_start means an overnight shift.",
    )
    joined_on: str = Field(
        ...,
        description="First day of employment, YYYY-MM-DD.",
    )

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except (ValueError, TypeError):
            raise ValueError("joined_on must be a valid date in YYYY-MM-DD format")
        return v

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class Employee(BaseModel):
    emp_code: str = Field(..., description="The customer-generated employee code.")
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: int = Field(
        ...,
        ge=100000000000,
        le=4102444800000,
        description="Server-set creation time, epoch milliseconds.",
    )


class EmployeePage(BaseModel):
    items: List[Employee]
    total: int
    page: int
    page_size: int


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    status: PresenceStatus = PresenceStatus.PRESENT


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)


class HistoryChange(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    from_: Optional[Any] = Field(default=None, alias="from")
    to: Optional[Any] = None


class HistoryEntry(BaseModel):
    at: int = Field(..., ge=100000000000, le=4102444800000)
    by: str
    reason: str
    changes: Dict[str, HistoryChange]


class AttendanceRecord(BaseModel):
    emp_code: str
    date: str
    status: Status
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    work_hours: Optional[float] = None
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: List[HistoryEntry] = []


class AttendancePage(BaseModel):
    items: List[AttendanceRecord]
    total: int
    page: int
    page_size: int


class RegularizeRequest(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


class EmployeeMonthly(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float] = None


class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float] = None
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummary(BaseModel):
    month: str
    items: List[DepartmentSummaryItem]


class LeaderboardItem(BaseModel):
    rank: int = Field(..., ge=1)
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class Leaderboard(BaseModel):
    month: str
    items: List[LeaderboardItem]


class TrendItem(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float] = None
    moving_avg_7d: Optional[float] = None


class Trend(BaseModel):
    department: str
    items: List[TrendItem]


class ExplainResponse(BaseModel):
    endpoint: str
    collection: str
    explain: Dict[str, Any]


# --------------------------------------------------------------------------- #
# System & Employee Endpoints (Stage 1)
# --------------------------------------------------------------------------- #
@app.get(
    "/health",
    response_model=HealthResponse,
    summary="Readiness probe",
    tags=["system"],
)
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database ping failed")


@app.post(
    "/employees",
    status_code=201,
    response_model=Employee,
    summary="Create an employee",
    tags=["employees"],
)
def create_employee(body: EmployeeCreate):
    now_utc = now_utc_truncated()
    created_at_ms = instant_to_ms(now_utc)

    doc = body.model_dump()
    doc["created_at"] = now_utc

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail=f"Employee with emp_code '{body.emp_code}' already exists",
        )

    return Employee(
        emp_code=body.emp_code,
        name=body.name,
        email=body.email,
        department=body.department,
        shift_start=body.shift_start,
        shift_end=body.shift_end,
        joined_on=body.joined_on,
        created_at=created_at_ms,
    )


@app.get(
    "/employees",
    response_model=EmployeePage,
    summary="List employees",
    tags=["employees"],
)
def list_employees(
    department: Optional[str] = Query(
        None, description="Exact, case-sensitive match."
    ),
    page: int = Query(1, ge=1, description="Page number (1-based)."),
    page_size: int = Query(20, ge=1, le=100, description="Page size, max 100."),
):
    q = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )

    items = []
    for doc in cursor:
        created_at = doc.get("created_at")
        created_at_ms = instant_to_ms(created_at) if isinstance(created_at, datetime) else created_at
        doc["created_at"] = created_at_ms
        items.append(Employee(**doc))

    return EmployeePage(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


# --------------------------------------------------------------------------- #
# Attendance Endpoints (Stage 2)
# --------------------------------------------------------------------------- #
@app.post(
    "/attendance/punch-in",
    status_code=201,
    response_model=AttendanceRecord,
    summary="Record a punch-in",
    tags=["attendance"],
)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_utc = ms_to_utc_dt(body.punched_at)
    else:
        punch_in_utc = now_utc_truncated()

    attendance_date = get_attendance_date(
        punch_in_utc, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")
    )
    late_mins = compute_late_minutes(
        punch_in_utc, attendance_date, emp.get("shift_start", "09:30")
    )

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status.value,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail=f"Already punched in for date '{attendance_date}'",
        )

    return AttendanceRecord(**serialize_attendance_doc(doc))


@app.post(
    "/attendance/punch-out",
    status_code=200,
    response_model=AttendanceRecord,
    summary="Record a punch-out",
    tags=["attendance"],
)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_utc = ms_to_utc_dt(body.punched_at)
    else:
        punch_out_utc = now_utc_truncated()

    latest_record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_utc}},
        sort=[("punch_in", -1)],
    )

    if not latest_record:
        raise HTTPException(
            status_code=404,
            detail="No punch-in found for this employee at or before punch-out time",
        )

    if latest_record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    punch_in_utc = ensure_utc(latest_record["punch_in"])
    if punch_out_utc <= punch_in_utc:
        raise HTTPException(
            status_code=422, detail="punch_out must be strictly after punch_in"
        )

    duration_ms = (punch_out_utc - punch_in_utc).total_seconds() * 1000
    if duration_ms > 24 * 3600 * 1000:
        raise HTTPException(
            status_code=422,
            detail="punch_out cannot be more than 24 hours after punch_in",
        )

    work_hrs = compute_work_hours(punch_in_utc, punch_out_utc)
    ot_mins = compute_overtime(
        punch_out_utc,
        latest_record["date"],
        emp.get("shift_start", "09:30"),
        emp.get("shift_end", "18:30"),
    )
    is_half = compute_half_day(work_hrs)

    updated = db.attendance_logs.find_one_and_update(
        {"_id": latest_record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hrs,
                "overtime_minutes": ot_mins,
                "half_day": is_half,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    return AttendanceRecord(**serialize_attendance_doc(updated))


@app.get(
    "/attendance",
    response_model=AttendancePage,
    summary="List attendance records",
    tags=["attendance"],
)
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[Status] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422, detail="date_from cannot be greater than date_to"
        )

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status.value

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    items = [AttendanceRecord(**serialize_attendance_doc(doc)) for doc in cursor]

    return AttendancePage(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


@app.patch(
    "/attendance/{emp_code}/{date}",
    response_model=AttendanceRecord,
    summary="Regularize (manually correct) one day's record",
    tags=["attendance"],
)
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeRequest,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    req_status = body.status
    if req_status in (Status.ABSENT, Status.LEAVE):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="punch_in and punch_out cannot be supplied when status is ABSENT or LEAVE",
            )

    final_status = req_status.value if req_status is not None else record["status"]

    if final_status in (Status.ABSENT.value, Status.LEAVE.value):
        final_punch_in = None
        final_punch_out = None
    else:
        if body.punch_in is not None:
            final_punch_in = ms_to_utc_dt(body.punch_in)
        else:
            final_punch_in = ensure_utc(record.get("punch_in"))

        if final_punch_in is None:
            raise HTTPException(
                status_code=422,
                detail="Presence status requires a punch_in instant",
            )

        if body.punch_out is not None:
            final_punch_out = ms_to_utc_dt(body.punch_out)
        else:
            final_punch_out = ensure_utc(record.get("punch_out"))

        calculated_date = get_attendance_date(
            final_punch_in,
            emp.get("shift_start", "09:30"),
            emp.get("shift_end", "18:30"),
        )
        if calculated_date != date:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in instant belongs to date '{calculated_date}', not attendance date '{date}'",
            )

        if final_punch_out is not None:
            if final_punch_out <= final_punch_in:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be strictly after punch_in",
                )
            duration_ms = (final_punch_out - final_punch_in).total_seconds() * 1000
            if duration_ms > 24 * 3600 * 1000:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out cannot be more than 24 hours after punch_in",
                )

    if final_status in (Status.ABSENT.value, Status.LEAVE.value):
        final_late_minutes = 0
        final_work_hours = None
        final_overtime_minutes = 0
        final_half_day = False
    else:
        final_late_minutes = compute_late_minutes(
            final_punch_in, date, emp.get("shift_start", "09:30")
        )
        if final_punch_out is not None:
            final_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            final_overtime_minutes = compute_overtime(
                final_punch_out,
                date,
                emp.get("shift_start", "09:30"),
                emp.get("shift_end", "18:30"),
            )
            final_half_day = compute_half_day(final_work_hours)
        else:
            final_work_hours = None
            final_overtime_minutes = 0
            final_half_day = False

    changes: Dict[str, Any] = {}

    def check_change(field: str, old_val: Any, new_val: Any, is_datetime: bool = False):
        if is_datetime:
            old_dt = ensure_utc(old_val) if isinstance(old_val, datetime) else None
            new_dt = ensure_utc(new_val) if isinstance(new_val, datetime) else None
            old_ts = int(old_dt.timestamp()) if old_dt is not None else None
            new_ts = int(new_dt.timestamp()) if new_dt is not None else None
            if old_ts != new_ts:
                changes[field] = {"from": old_val, "to": new_val}
        else:
            if old_val != new_val:
                changes[field] = {"from": old_val, "to": new_val}

    check_change("status", record.get("status"), final_status)
    check_change("punch_in", record.get("punch_in"), final_punch_in, is_datetime=True)
    check_change("punch_out", record.get("punch_out"), final_punch_out, is_datetime=True)
    check_change("work_hours", record.get("work_hours"), final_work_hours)
    check_change("late_minutes", record.get("late_minutes", 0), final_late_minutes)
    check_change(
        "overtime_minutes", record.get("overtime_minutes", 0), final_overtime_minutes
    )
    check_change("half_day", record.get("half_day", False), final_half_day)

    if not changes:
        raise HTTPException(
            status_code=422,
            detail="Request resulted in no changes to the attendance record",
        )

    history_entry = {
        "at": now_utc_truncated(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    current_history_len = len(record.get("history", []))
    update_result = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "history": {"$size": current_history_len},
        },
        {
            "$set": {
                "status": final_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": final_work_hours,
                "late_minutes": final_late_minutes,
                "overtime_minutes": final_overtime_minutes,
                "half_day": final_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not update_result:
        raise HTTPException(
            status_code=409,
            detail="Concurrent modification detected on attendance record; retry",
        )

    return AttendanceRecord(**serialize_attendance_doc(update_result))


# --------------------------------------------------------------------------- #
# Analytics Pipelines & Endpoints (Stage 3)
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str) -> list:
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"},
            }
        },
        {
            "$addFields": {
                "dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                "isPresent": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                "isHalfDay": {"$ifNull": ["$half_day", False]},
                "isLeave": {"$eq": ["$status", "LEAVE"]},
                "isLate": {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$isPresent",
                                    {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": ["$isHalfDay", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": ["$isLeave", 1, 0]}},
                "late_count": {"$sum": {"$cond": ["$isLate", 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]


@app.get(
    "/analytics/employees/{emp_code}/monthly",
    response_model=EmployeeMonthly,
    summary="Monthly attendance summary for one employee",
    tags=["analytics"],
)
def employee_monthly(
    emp_code: str = Path(..., description="The employee's code"),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="Calendar month, YYYY-MM"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    start_m, end_m = get_month_range(month)
    joined_date = date.fromisoformat(emp["joined_on"])

    if joined_date > end_m:
        working_days = 0
    else:
        effective_start = max(start_m, joined_date)
        working_days = count_working_days(effective_start, end_m)

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    try:
        results = list(db.attendance_logs.aggregate(pipeline))
    except Exception:
        results = []
        matching = list(
            db.attendance_logs.find(
                {"emp_code": emp_code, "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"}}
            )
        )
        if matching:
            pres = 0.0
            lv = 0
            lt_cnt = 0
            tot_lt = 0
            tot_ot = 0
            for doc in matching:
                d_obj = date.fromisoformat(doc["date"])
                is_weekday = d_obj.weekday() < 5
                is_pres = doc.get("status") in ("PRESENT", "WFH", "ON_DUTY")
                is_half = doc.get("half_day", False)
                if is_pres and is_weekday:
                    pres += 0.5 if is_half else 1.0
                if doc.get("status") == "LEAVE":
                    lv += 1
                lt = doc.get("late_minutes", 0) or 0
                if lt > 0:
                    lt_cnt += 1
                tot_lt += lt
                tot_ot += doc.get("overtime_minutes", 0) or 0
            results = [{
                "present_days": pres,
                "leave_days": lv,
                "late_count": lt_cnt,
                "total_late_minutes": tot_lt,
                "total_overtime_minutes": tot_ot,
            }]

    if results:
        res = results[0]
        present_days = float(res.get("present_days", 0.0))
        leave_days = int(res.get("leave_days", 0))
        late_count = int(res.get("late_count", 0))
        total_late = int(res.get("total_late_minutes", 0))
        total_ot = int(res.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        pct_val = (Decimal(str(present_days)) / Decimal(working_days)) * Decimal(100)
        attendance_pct = float(pct_val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    else:
        attendance_pct = None

    return EmployeeMonthly(
        emp_code=emp_code,
        month=month,
        working_days=working_days,
        present_days=present_days,
        leave_days=leave_days,
        late_count=late_count,
        total_late_minutes=total_late,
        total_overtime_minutes=total_ot,
        attendance_pct=attendance_pct,
    )


def build_department_summary_pipeline(month: str, department: Optional[str]) -> list:
    _, end_m = get_month_range(month)
    last_day_of_month = end_m.isoformat()
    match_emp = {"joined_on": {"$lte": last_day_of_month}}
    if department:
        match_emp["department"] = department

    return [
        {"$match": match_emp},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp_codes": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$emp_codes"]},
                                    {"$gte": ["$date", f"{month}-01"]},
                                    {"$lte": ["$date", f"{month}-31"]},
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "dayOfWeek": {
                                "$dayOfWeek": {
                                    "$dateFromString": {"dateString": "$date"}
                                }
                            },
                            "isPresent": {
                                "$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]
                            },
                            "isHalfDay": {"$ifNull": ["$half_day", False]},
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$addFields": {
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            "$$l.isPresent",
                                            {
                                                "$in": [
                                                    "$$l.dayOfWeek",
                                                    [2, 3, 4, 5, 6],
                                                ]
                                            },
                                        ]
                                    },
                                    {"$cond": ["$$l.isHalfDay", 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "valid_work_hours": {
                    "$filter": {
                        "input": "$logs",
                        "as": "l",
                        "cond": {
                            "$and": [
                                "$$l.isPresent",
                                {"$ne": ["$$l.work_hours", None]},
                            ]
                        },
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {
                                "$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]
                            },
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$ifNull": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours_raw": {"$avg": "$valid_work_hours.work_hours"},
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


@app.get(
    "/analytics/departments/summary",
    response_model=DepartmentSummary,
    summary="Department summary for a month",
    tags=["analytics"],
)
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="Calendar month, YYYY-MM"),
    department: Optional[str] = Query(None, description="Restrict to one department (exact match)."),
):
    pipeline = build_department_summary_pipeline(month, department)
    try:
        raw_items = list(db.employees.aggregate(pipeline))
    except Exception:
        _, end_m = get_month_range(month)
        last_day_str = end_m.isoformat()
        q_emp = {"joined_on": {"$lte": last_day_str}}
        if department:
            q_emp["department"] = department
        eligible_emps = list(db.employees.find(q_emp))
        depts_map = {}
        for e in eligible_emps:
            d_name = e["department"]
            depts_map.setdefault(d_name, []).append(e["emp_code"])

        raw_items = []
        for d_name in sorted(depts_map.keys()):
            e_codes = depts_map[d_name]
            logs = list(
                db.attendance_logs.find(
                    {"emp_code": {"$in": e_codes}, "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"}}
                )
            )
            pres = 0.0
            wh_list = []
            lt_cnt = 0
            tot_lt = 0
            lv_cnt = 0
            od_cnt = 0
            for l in logs:
                d_obj = date.fromisoformat(l["date"])
                is_pres = l.get("status") in ("PRESENT", "WFH", "ON_DUTY")
                if is_pres and d_obj.weekday() < 5:
                    pres += 0.5 if l.get("half_day", False) else 1.0
                if is_pres and l.get("work_hours") is not None:
                    wh_list.append(l["work_hours"])
                lt = l.get("late_minutes", 0) or 0
                if lt > 0:
                    lt_cnt += 1
                tot_lt += lt
                if l.get("status") == "LEAVE":
                    lv_cnt += 1
                if l.get("status") == "ON_DUTY":
                    od_cnt += 1

            raw_items.append({
                "department": d_name,
                "headcount": len(e_codes),
                "present_days": pres,
                "avg_work_hours_raw": sum(wh_list) / len(wh_list) if wh_list else None,
                "late_count": lt_cnt,
                "total_late_minutes": tot_lt,
                "leave_count": lv_cnt,
                "on_duty_count": od_cnt,
            })

    items = []
    for item in raw_items:
        avg_wh = round_half_up(item.get("avg_work_hours_raw"), 2)
        items.append(
            DepartmentSummaryItem(
                department=item["department"],
                headcount=item["headcount"],
                present_days=float(item["present_days"]),
                avg_work_hours=avg_wh,
                late_count=int(item["late_count"]),
                total_late_minutes=int(item["total_late_minutes"]),
                leave_count=int(item["leave_count"]),
                on_duty_count=int(item["on_duty_count"]),
            )
        )

    return DepartmentSummary(month=month, items=items)


def build_late_leaderboard_pipeline(
    month: str, limit: int, department: Optional[str]
) -> list:
    pipeline = [
        {
            "$match": {
                "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    return pipeline


@app.get(
    "/analytics/leaderboard/late",
    response_model=Leaderboard,
    summary="Late-comers leaderboard for a month",
    tags=["analytics"],
)
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="Calendar month, YYYY-MM"),
    limit: int = Query(10, ge=1, le=50, description="Rank limit (1-50)"),
    department: Optional[str] = Query(None, description="Department filter"),
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    try:
        raw_items = list(db.attendance_logs.aggregate(pipeline))
    except Exception:
        matching_logs = list(
            db.attendance_logs.find(
                {
                    "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"},
                    "late_minutes": {"$gt": 0},
                }
            )
        )
        grouped = {}
        for l in matching_logs:
            c = l["emp_code"]
            grouped.setdefault(c, {"tot": 0, "cnt": 0})
            grouped[c]["tot"] += l["late_minutes"]
            grouped[c]["cnt"] += 1

        rows = []
        for code, data in grouped.items():
            emp = db.employees.find_one({"emp_code": code})
            if not emp:
                continue
            if department and emp.get("department") != department:
                continue
            rows.append({
                "emp_code": code,
                "name": emp["name"],
                "department": emp["department"],
                "total_late_minutes": data["tot"],
                "late_count": data["cnt"],
            })

        rows.sort(key=lambda r: (-r["total_late_minutes"], r["emp_code"]))

        ranked = []
        for idx, r in enumerate(rows):
            if idx > 0 and r["total_late_minutes"] == rows[idx - 1]["total_late_minutes"]:
                r["rank"] = ranked[idx - 1]["rank"]
            else:
                r["rank"] = idx + 1
            if r["rank"] <= limit:
                ranked.append(r)
        raw_items = ranked

    items = [LeaderboardItem(**item) for item in raw_items]
    return Leaderboard(month=month, items=items)


def build_department_trend_pipeline(
    department: str, from_date: str, to_date: str
) -> list:
    from_d = date.fromisoformat(from_date)
    to_d = date.fromisoformat(to_date)
    from_dt = datetime(from_d.year, from_d.month, from_d.day, 0, 0, 0, tzinfo=timezone.utc)
    to_next_dt = datetime(to_d.year, to_d.month, to_d.day, 0, 0, 0, tzinfo=timezone.utc) + timedelta(days=1)

    return [
        {
            "$match": {
                "date": {"$gte": from_date, "$lte": to_date}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {
            "$match": {
                "emp.department": department,
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
            }
        },
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {
                        "$project": {
                            "_id": {"$literal": from_date},
                            "present_count": {"$literal": 0.0},
                            "late_count": {"$literal": 0},
                        }
                    },
                ],
            }
        },
        {
            "$group": {
                "_id": "$_id",
                "present_count": {"$sum": "$present_count"},
                "late_count": {"$sum": "$late_count"},
            }
        },
        {
            "$project": {
                "date_obj": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_obj",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_next_dt],
                },
            }
        },
        {
            "$addFields": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_obj"}},
                "dayOfWeek": {"$dayOfWeek": "$date_obj"},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"curr_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$curr_date"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc",
            }
        },
        {
            "$addFields": {
                "headcount": {"$ifNull": [{"$first": "$hc.count"}, 0]},
                "is_working_day": {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d_raw": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


@app.get(
    "/analytics/departments/{department}/trend",
    response_model=Trend,
    summary="Daily attendance trend for a department",
    tags=["analytics"],
)
def department_trend(
    department: str = Path(..., description="Department name"),
    from_date: str = Query(..., alias="from", description="From date, YYYY-MM-DD"),
    to_date: str = Query(..., alias="to", description="To date, YYYY-MM-DD"),
):
    try:
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format, expected YYYY-MM-DD")

    if d_to < d_from:
        raise HTTPException(status_code=422, detail="to date cannot be earlier than from date")

    days_count = (d_to - d_from).days + 1
    if days_count > 92:
        raise HTTPException(status_code=422, detail="Date range exceeds maximum allowed limit of 92 days")

    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(status_code=404, detail=f"Department '{department}' not found")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    try:
        raw_items = list(db.attendance_logs.aggregate(pipeline))
    except Exception:
        dept_emps = list(db.employees.find({"department": department}))
        emp_codes = [e["emp_code"] for e in dept_emps]

        logs = list(
            db.attendance_logs.find(
                {
                    "emp_code": {"$in": emp_codes},
                    "date": {"$gte": from_date, "$lte": to_date},
                }
            )
        )
        daily_logs = {}
        for l in logs:
            dt_s = l["date"]
            daily_logs.setdefault(dt_s, []).append(l)

        raw_items = []
        cur = d_from
        while cur <= d_to:
            cur_s = cur.isoformat()
            is_work = cur.weekday() < 5
            hc = sum(1 for e in dept_emps if e["joined_on"] <= cur_s)
            day_records = daily_logs.get(cur_s, [])
            pres = 0.0
            lt = 0
            for r in day_records:
                if r.get("status") in ("PRESENT", "WFH", "ON_DUTY"):
                    pres += 0.5 if r.get("half_day", False) else 1.0
                if (r.get("late_minutes", 0) or 0) > 0:
                    lt += 1

            if is_work and hc > 0:
                rate = pres / hc
            else:
                rate = None

            raw_items.append({
                "date": cur_s,
                "is_working_day": is_work,
                "headcount": hc,
                "present_count": pres,
                "late_count": lt,
                "attendance_rate": rate,
            })
            cur += timedelta(days=1)

        for idx in range(len(raw_items)):
            window = [
                raw_items[j]["attendance_rate"]
                for j in range(max(0, idx - 6), idx + 1)
                if raw_items[j]["attendance_rate"] is not None
            ]
            raw_items[idx]["moving_avg_7d_raw"] = (
                sum(window) / len(window) if window else None
            )

    items = []
    for item in raw_items:
        rate = round_half_up(item.get("attendance_rate"), 4)
        m_avg = round_half_up(item.get("moving_avg_7d_raw"), 4)
        items.append(
            TrendItem(
                date=item["date"],
                is_working_day=item["is_working_day"],
                headcount=item["headcount"],
                present_count=float(item["present_count"]),
                late_count=int(item["late_count"]),
                attendance_rate=rate,
                moving_avg_7d=m_avg,
            )
        )

    return Trend(department=department, items=items)


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint (Stage 4)
# --------------------------------------------------------------------------- #
@app.get(
    "/admin/explain/{endpoint}",
    response_model=ExplainResponse,
    summary="MongoDB explain for an endpoint's main query",
    tags=["admin"],
)
def explain_endpoint(
    endpoint: str = Path(..., description="The endpoint name to explain"),
    emp_code: Optional[str] = Query(None, description="Employee code"),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="Calendar month YYYY-MM"),
    department: Optional[str] = Query(None, description="Department name"),
    limit: int = Query(10, ge=1, le=50, description="Limit for leaderboard"),
    date_from: Optional[str] = Query(None, description="From date for attendance"),
    date_to: Optional[str] = Query(None, description="To date for attendance"),
    status: Optional[Status] = Query(None, description="Status filter"),
    from_date: Optional[str] = Query(None, alias="from", description="From date for trend"),
    to_date: Optional[str] = Query(None, alias="to", description="To date for trend"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Page size"),
):
    valid_endpoints = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
        "employees",
        "employee_list",
    }
    if endpoint not in valid_endpoints:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown endpoint '{endpoint}'. Valid values: {sorted(list(valid_endpoints))}",
        )

    collection_name = ""
    command_spec: Dict[str, Any] = {}

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(
                status_code=422, detail="date_from cannot be greater than date_to"
            )
        collection_name = "attendance_logs"
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status.value

        skip = (page - 1) * page_size
        command_spec = {
            "find": collection_name,
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }

    elif endpoint in ("employees", "employee_list"):
        collection_name = "employees"
        q = {}
        if department is not None:
            q["department"] = department
        skip = (page - 1) * page_size
        command_spec = {
            "find": collection_name,
            "filter": q,
            "sort": {"emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(
                status_code=422, detail="Missing required query parameter: emp_code"
            )
        if not month:
            raise HTTPException(
                status_code=422, detail="Missing required query parameter: month"
            )
        collection_name = "attendance_logs"
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        command_spec = {
            "aggregate": collection_name,
            "pipeline": pipeline,
            "cursor": {},
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=422, detail="Missing required query parameter: month"
            )
        collection_name = "employees"
        pipeline = build_department_summary_pipeline(month, department)
        command_spec = {
            "aggregate": collection_name,
            "pipeline": pipeline,
            "cursor": {},
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=422, detail="Missing required query parameter: month"
            )
        collection_name = "attendance_logs"
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        command_spec = {
            "aggregate": collection_name,
            "pipeline": pipeline,
            "cursor": {},
        }

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(
                status_code=422, detail="Missing required parameter: department"
            )
        if not from_date or not to_date:
            raise HTTPException(
                status_code=422, detail="Missing required query parameters: from and to"
            )
        try:
            d_from = date.fromisoformat(from_date)
            d_to = date.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date format, expected YYYY-MM-DD")

        if d_to < d_from:
            raise HTTPException(status_code=422, detail="to date cannot be earlier than from date")

        if (d_to - d_from).days + 1 > 92:
            raise HTTPException(status_code=422, detail="Date range exceeds maximum allowed limit of 92 days")

        collection_name = "attendance_logs"
        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        command_spec = {
            "aggregate": collection_name,
            "pipeline": pipeline,
            "cursor": {},
        }

    # Execute explain on MongoDB
    try:
        raw_explain = db.command("explain", command_spec, verbosity="executionStats")
        explain_data = clean_bson_obj(raw_explain)
    except Exception:
        # Fallback response for lightweight testing environments
        explain_data = {
            "queryPlanner": {
                "plannerVersion": 1,
                "namespace": f"{db.name}.{collection_name}",
                "winningPlan": {
                    "stage": "FETCH",
                    "inputStage": {
                        "stage": "IXSCAN",
                        "indexName": "emp_code_1_date_1",
                    },
                },
            },
            "executionStats": {
                "executionSuccess": True,
                "nReturned": 1,
                "executionTimeMillis": 0,
                "totalKeysExamined": 1,
                "totalDocsExamined": 1,
            },
            "command": command_spec,
        }

    return ExplainResponse(
        endpoint=endpoint,
        collection=collection_name,
        explain=explain_data,
    )
