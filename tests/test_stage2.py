import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import mongomock
import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import (
    app,
    db,
    client as mongo_client,
    ensure_indexes,
    instant_to_ms,
    ms_to_utc_dt,
    compute_late_minutes,
    compute_work_hours,
    compute_overtime,
    compute_half_day,
    get_attendance_date,
    IST,
)

test_client = TestClient(app)


def is_live_mongo_available():
    try:
        main_module.client.admin.command("ping")
        return True
    except Exception:
        return False


LIVE_AVAILABLE = is_live_mongo_available()


@pytest.fixture(autouse=True)
def setup_teardown_db():
    if not LIVE_AVAILABLE:
        mock_client = mongomock.MongoClient()
        mock_db = mock_client["attendance_db"]
        with patch_mongo(mock_client, mock_db):
            main_module.ensure_indexes()
            setup_test_employees()
            yield
    else:
        main_module.ensure_indexes()
        setup_test_employees()
        yield
        main_module.db.employees.delete_many({"emp_code": {"$regex": "^EMP88"}})
        main_module.db.attendance_logs.delete_many({"emp_code": {"$regex": "^EMP88"}})


from contextlib import contextmanager
from unittest.mock import patch


@contextmanager
def patch_mongo(mock_client, mock_db):
    with patch.object(main_module, "client", mock_client), patch.object(
        main_module, "db", mock_db
    ):
        yield


def setup_test_employees():
    # Setup regular and overnight employees
    employees = [
        {
            "emp_code": "EMP8801",
            "name": "Standard Shift Emp",
            "email": "emp8801@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
            "created_at": datetime.now(timezone.utc),
        },
        {
            "emp_code": "EMP8802",
            "name": "Overnight Shift Emp",
            "email": "emp8802@example.com",
            "department": "Support",
            "shift_start": "22:00",
            "shift_end": "06:00",
            "joined_on": "2026-01-01",
            "created_at": datetime.now(timezone.utc),
        },
    ]
    for emp in employees:
        main_module.db.employees.delete_one({"emp_code": emp["emp_code"]})
        main_module.db.employees.insert_one(emp)


# --------------------------------------------------------------------------- #
# Helper Unit Tests: Boundaries & Rounding
# --------------------------------------------------------------------------- #
def test_grace_period_boundaries():
    # Shift start 09:30 IST on 2026-07-06
    # 09:40:00 -> exactly 10 min -> not late
    dt_10m00s = datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST).astimezone(timezone.utc)
    assert compute_late_minutes(dt_10m00s, "2026-07-06", "09:30") == 0

    # 09:40:01 -> strictly > 10 min -> late = 10 min
    dt_10m01s = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST).astimezone(timezone.utc)
    assert compute_late_minutes(dt_10m01s, "2026-07-06", "09:30") == 10

    # 10:15:59 -> 45 min late
    dt_45m59s = datetime(2026, 7, 6, 10, 15, 59, tzinfo=IST).astimezone(timezone.utc)
    assert compute_late_minutes(dt_45m59s, "2026-07-06", "09:30") == 45


def test_overtime_threshold():
    # Regular shift ends 18:30 IST on 2026-07-06
    # 18:55 (25 min OT) -> below 30 min minimum -> 0
    dt_25m = datetime(2026, 7, 6, 18, 55, 0, tzinfo=IST).astimezone(timezone.utc)
    assert compute_overtime(dt_25m, "2026-07-06", "09:30", "18:30") == 0

    # 19:00 (30 min OT) -> >= 30 -> 30
    dt_30m = datetime(2026, 7, 6, 19, 0, 0, tzinfo=IST).astimezone(timezone.utc)
    assert compute_overtime(dt_30m, "2026-07-06", "09:30", "18:30") == 30

    # 19:10 (40 min OT) -> 40
    dt_40m = datetime(2026, 7, 6, 19, 10, 0, tzinfo=IST).astimezone(timezone.utc)
    assert compute_overtime(dt_40m, "2026-07-06", "09:30", "18:30") == 40


def test_work_hours_half_up_and_half_day():
    # 4 hours 29 minutes 42 seconds = 16182 seconds
    # 16182 / 3600 = 4.495 -> Half-up rounds to 4.50!
    t1 = datetime(2026, 7, 6, 9, 0, 0, tzinfo=timezone.utc)
    t2 = t1 + timedelta(seconds=16182)
    wh = compute_work_hours(t1, t2)
    assert wh == 4.50
    assert compute_half_day(wh) is False  # 4.50 is not < 4.50

    # 16181 seconds / 3600 = 4.49472... -> Half-up rounds to 4.49
    t3 = t1 + timedelta(seconds=16181)
    wh_half = compute_work_hours(t1, t3)
    assert wh_half == 4.49
    assert compute_half_day(wh_half) is True  # 4.49 < 4.50 -> half day


def test_overnight_date_attribution():
    # Overnight shift: 22:00 to 06:00
    # Punch in 21:55 IST on 2026-07-06
    p1 = datetime(2026, 7, 6, 21, 55, 0, tzinfo=IST).astimezone(timezone.utc)
    assert get_attendance_date(p1, "22:00", "06:00") == "2026-07-06"

    # Punch in 01:10 IST on 2026-07-07 (earlier than 06:00 shift_end)
    p2 = datetime(2026, 7, 7, 1, 10, 0, tzinfo=IST).astimezone(timezone.utc)
    # Belongs to previous day: 2026-07-06
    assert get_attendance_date(p2, "22:00", "06:00") == "2026-07-06"


# --------------------------------------------------------------------------- #
# Endpoint Tests: Punch-in
# --------------------------------------------------------------------------- #
def test_punch_in_success():
    # 09:28 IST on 2026-07-06: 03:58 UTC
    in_dt = datetime(2026, 7, 6, 9, 28, 0, tzinfo=IST)
    in_ms = instant_to_ms(in_dt)

    res = test_client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP8801", "punched_at": in_ms, "status": "PRESENT"},
    )
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP8801"
    assert data["date"] == "2026-07-06"
    assert data["status"] == "PRESENT"
    assert data["punch_in"] == in_ms
    assert data["punch_out"] is None
    assert data["work_hours"] is None
    assert data["late_minutes"] == 0
    assert data["overtime_minutes"] == 0
    assert data["half_day"] is False
    assert data["history"] == []


def test_punch_in_unknown_employee():
    res = test_client.post("/attendance/punch-in", json={"emp_code": "UNKNOWN999"})
    assert res.status_code == 404


def test_punch_in_duplicate_same_date():
    in_dt = datetime(2026, 7, 6, 9, 28, 0, tzinfo=IST)
    in_ms = instant_to_ms(in_dt)

    r1 = test_client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP8801", "punched_at": in_ms, "status": "PRESENT"},
    )
    assert r1.status_code == 201

    r2 = test_client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP8801", "punched_at": in_ms + 1000, "status": "PRESENT"},
    )
    assert r2.status_code == 409


def test_punch_in_concurrent_requests():
    in_dt = datetime(2026, 7, 7, 9, 28, 0, tzinfo=IST)
    in_ms = instant_to_ms(in_dt)
    payload = {"emp_code": "EMP8801", "punched_at": in_ms, "status": "PRESENT"}

    results = []
    lock = threading.Lock()

    def send_req():
        res = test_client.post("/attendance/punch-in", json=payload)
        with lock:
            results.append(res.status_code)

    threads = [threading.Thread(target=send_req) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(201) == 1
    assert results.count(409) == 4


# --------------------------------------------------------------------------- #
# Endpoint Tests: Punch-out
# --------------------------------------------------------------------------- #
def test_punch_out_success():
    in_dt = datetime(2026, 7, 8, 9, 28, 0, tzinfo=IST)
    out_dt = datetime(2026, 7, 8, 18, 35, 0, tzinfo=IST)
    in_ms = instant_to_ms(in_dt)
    out_ms = instant_to_ms(out_dt)

    test_client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP8801", "punched_at": in_ms, "status": "PRESENT"},
    )

    res = test_client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP8801", "punched_at": out_ms},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["punch_out"] == out_ms
    assert data["work_hours"] == 9.12
    assert data["overtime_minutes"] == 0  # 5 min is under 30
    assert data["half_day"] is False


def test_punch_out_overnight_shift():
    # Punch in 21:55 IST on 2026-07-06
    in_dt = datetime(2026, 7, 6, 21, 55, 0, tzinfo=IST)
    # Punch out 06:40 IST on 2026-07-07 (next day)
    out_dt = datetime(2026, 7, 7, 6, 40, 0, tzinfo=IST)

    test_client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP8802", "punched_at": instant_to_ms(in_dt)},
    )

    res = test_client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP8802", "punched_at": instant_to_ms(out_dt)},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["date"] == "2026-07-06"
    assert data["work_hours"] == 8.75
    assert data["overtime_minutes"] == 40  # 40 min past 06:00
    assert data["half_day"] is False


def test_punch_out_validation_failures():
    in_dt = datetime(2026, 7, 9, 9, 30, 0, tzinfo=IST)
    test_client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP8801", "punched_at": instant_to_ms(in_dt)},
    )

    # punch_out <= punch_in -> 422
    res_before = test_client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP8801", "punched_at": instant_to_ms(in_dt)},
    )
    assert res_before.status_code == 422

    # punch_out > 24 hours -> 422
    res_25h = test_client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP8801", "punched_at": instant_to_ms(in_dt + timedelta(hours=25))},
    )
    assert res_25h.status_code == 422


def test_punch_out_concurrency_race():
    in_dt = datetime(2026, 7, 10, 9, 30, 0, tzinfo=IST)
    out_dt = datetime(2026, 7, 10, 18, 30, 0, tzinfo=IST)
    test_client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP8801", "punched_at": instant_to_ms(in_dt)},
    )

    results = []
    lock = threading.Lock()

    def send_out():
        res = test_client.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP8801", "punched_at": instant_to_ms(out_dt)},
        )
        with lock:
            results.append(res.status_code)

    threads = [threading.Thread(target=send_out) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(200) == 1
    assert results.count(409) == 4


# --------------------------------------------------------------------------- #
# Endpoint Tests: Listing
# --------------------------------------------------------------------------- #
def test_list_attendance_filters_and_sorting():
    # Insert attendance records across dates
    dates = ["2026-07-01", "2026-07-02", "2026-07-03"]
    for d in dates:
        main_module.db.attendance_logs.delete_one({"emp_code": "EMP8801", "date": d})
        dt = datetime.fromisoformat(d).replace(hour=4, minute=0, tzinfo=timezone.utc)
        main_module.db.attendance_logs.insert_one({
            "emp_code": "EMP8801",
            "date": d,
            "status": "PRESENT",
            "punch_in": dt,
            "punch_out": dt + timedelta(hours=9),
            "work_hours": 9.0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        })

    # Range filter
    res = test_client.get("/attendance?emp_code=EMP8801&date_from=2026-07-02&date_to=2026-07-03")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 2
    # Verify sorting by date descending
    assert data["items"][0]["date"] == "2026-07-03"
    assert data["items"][1]["date"] == "2026-07-02"

    # date_from > date_to -> 422
    res_err = test_client.get("/attendance?date_from=2026-07-05&date_to=2026-07-01")
    assert res_err.status_code == 422


# --------------------------------------------------------------------------- #
# Endpoint Tests: Regularization
# --------------------------------------------------------------------------- #
def test_regularize_attendance_success():
    # Create record
    in_dt = datetime(2026, 7, 11, 10, 5, 0, tzinfo=IST)  # 35 min late
    out_dt = datetime(2026, 7, 11, 19, 10, 0, tzinfo=IST)
    test_client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP8801", "punched_at": instant_to_ms(in_dt)},
    )
    test_client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP8801", "punched_at": instant_to_ms(out_dt)},
    )

    # Correct punch_in to 09:28 IST (inside grace period -> 0 late)
    corrected_in = datetime(2026, 7, 11, 9, 28, 0, tzinfo=IST)
    patch_res = test_client.patch(
        "/attendance/EMP8801/2026-07-11",
        json={
            "punch_in": instant_to_ms(corrected_in),
            "reason": "Biometric machine lag",
            "regularized_by": "hr.admin",
        },
    )
    assert patch_res.status_code == 200
    data = patch_res.json()
    assert data["late_minutes"] == 0
    assert len(data["history"]) == 1
    h = data["history"][0]
    assert h["by"] == "hr.admin"
    assert h["reason"] == "Biometric machine lag"
    assert "punch_in" in h["changes"]
    assert "late_minutes" in h["changes"]
    assert h["changes"]["late_minutes"]["from"] == 35
    assert h["changes"]["late_minutes"]["to"] == 0


def test_regularize_absent_clears_punches():
    in_dt = datetime(2026, 7, 12, 9, 30, 0, tzinfo=IST)
    test_client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP8801", "punched_at": instant_to_ms(in_dt)},
    )

    # Supplying punches with ABSENT -> 422
    bad_res = test_client.patch(
        "/attendance/EMP8801/2026-07-12",
        json={
            "status": "ABSENT",
            "punch_in": instant_to_ms(in_dt),
            "reason": "Mark absent",
            "regularized_by": "hr.admin",
        },
    )
    assert bad_res.status_code == 422

    # Setting ABSENT clears punches
    ok_res = test_client.patch(
        "/attendance/EMP8801/2026-07-12",
        json={
            "status": "ABSENT",
            "reason": "Mark absent with approved status",
            "regularized_by": "hr.admin",
        },
    )
    assert ok_res.status_code == 200
    data = ok_res.json()
    assert data["status"] == "ABSENT"
    assert data["punch_in"] is None
    assert data["punch_out"] is None


def test_regularize_no_change_422():
    in_dt = datetime(2026, 7, 13, 9, 30, 0, tzinfo=IST)
    test_client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP8801", "punched_at": instant_to_ms(in_dt)},
    )

    # Send identical punch_in -> changes nothing
    res = test_client.patch(
        "/attendance/EMP8801/2026-07-13",
        json={
            "punch_in": instant_to_ms(in_dt),
            "reason": "Accidental re-save",
            "regularized_by": "hr.admin",
        },
    )
    assert res.status_code == 422


def test_punch_out_no_punches_at_all_404():
    # EMP8802 has no punch-in yet on this date
    res = test_client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP8802", "punched_at": 1783312500000},
    )
    assert res.status_code == 404


def test_regularize_not_found():
    # Unknown employee
    r1 = test_client.patch(
        "/attendance/UNKNOWN999/2026-07-01",
        json={"reason": "Valid reason here", "regularized_by": "admin"},
    )
    assert r1.status_code == 404

    # Existing employee but missing date record
    r2 = test_client.patch(
        "/attendance/EMP8801/2026-01-01",
        json={"reason": "Valid reason here", "regularized_by": "admin"},
    )
    assert r2.status_code == 404


def test_attendance_list_status_filter_and_empty_page():
    # Filter by status
    res = test_client.get("/attendance?status=PRESENT&page=1&page_size=20")
    assert res.status_code == 200

    # Page out of range returns empty items with accurate total
    res_empty = test_client.get("/attendance?page=999&page_size=20")
    assert res_empty.status_code == 200
    assert res_empty.json()["items"] == []
    assert res_empty.json()["page"] == 999
