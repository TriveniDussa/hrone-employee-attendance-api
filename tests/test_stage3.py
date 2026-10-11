from datetime import datetime, timezone, timedelta
from unittest.mock import patch
import mongomock
import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app, ensure_indexes

test_client = TestClient(app)


def is_live_mongo_available():
    try:
        main_module.client.admin.command("ping")
        return True
    except Exception:
        return False


LIVE_AVAILABLE = is_live_mongo_available()


@pytest.fixture(autouse=True)
def setup_teardown_analytics_data():
    if not LIVE_AVAILABLE:
        mock_client = mongomock.MongoClient()
        mock_db = mock_client["attendance_db"]
        with patch.object(main_module, "client", mock_client), patch.object(
            main_module, "db", mock_db
        ):
            main_module.ensure_indexes()
            seed_analytics_test_data()
            yield
    else:
        main_module.ensure_indexes()
        seed_analytics_test_data()
        yield
        main_module.db.employees.delete_many({"emp_code": {"$regex": "^EMP77"}})
        main_module.db.attendance_logs.delete_many({"emp_code": {"$regex": "^EMP77"}})


def seed_analytics_test_data():
    # Employees:
    # EMP7701: Engineering, joined 2026-07-06 (mid-month)
    # EMP7702: Engineering, joined 2026-01-01 (full month)
    # EMP7703: Engineering, joined 2026-08-01 (joined next month)
    # EMP7704: Marketing, joined 2026-07-01 (zero logs)
    # EMP7705: Sales, joined 2026-01-01 (late leaderboard tie 1)
    # EMP7706: Sales, joined 2026-01-01 (late leaderboard tie 2)
    # EMP7707: Sales, joined 2026-01-01 (late leaderboard rank 3)
    emps = [
        {
            "emp_code": "EMP7701",
            "name": "Mid Month Joiner",
            "email": "emp7701@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-06",
            "created_at": datetime.now(timezone.utc),
        },
        {
            "emp_code": "EMP7702",
            "name": "Full Month Engineer",
            "email": "emp7702@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
            "created_at": datetime.now(timezone.utc),
        },
        {
            "emp_code": "EMP7703",
            "name": "Future Joiner",
            "email": "emp7703@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-08-01",
            "created_at": datetime.now(timezone.utc),
        },
        {
            "emp_code": "EMP7704",
            "name": "Zero Log Marketer",
            "email": "emp7704@example.com",
            "department": "Marketing",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01",
            "created_at": datetime.now(timezone.utc),
        },
        {
            "emp_code": "EMP7705",
            "name": "Sales Tie One",
            "email": "emp7705@example.com",
            "department": "Sales",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
            "created_at": datetime.now(timezone.utc),
        },
        {
            "emp_code": "EMP7706",
            "name": "Sales Tie Two",
            "email": "emp7706@example.com",
            "department": "Sales",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
            "created_at": datetime.now(timezone.utc),
        },
        {
            "emp_code": "EMP7707",
            "name": "Sales Bronze",
            "email": "emp7707@example.com",
            "department": "Sales",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
            "created_at": datetime.now(timezone.utc),
        },
    ]
    for e in emps:
        main_module.db.employees.delete_one({"emp_code": e["emp_code"]})
        main_module.db.employees.insert_one(e)

    # Attendance logs:
    # For EMP7701 in 2026-07:
    # 2026-07-06 (Monday): PRESENT, work_hours 9.0, late 0
    # 2026-07-07 (Tuesday): WFH, half_day True, work_hours 3.5, late 15
    # 2026-07-08 (Wednesday): LEAVE
    # 2026-07-11 (Saturday - Weekend!): PRESENT, work_hours 8.0, late 20, overtime 40
    logs = [
        {
            "emp_code": "EMP7701",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc),
            "work_hours": 9.0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        },
        {
            "emp_code": "EMP7701",
            "date": "2026-07-07",
            "status": "WFH",
            "punch_in": datetime(2026, 7, 7, 4, 15, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, 7, 7, 45, tzinfo=timezone.utc),
            "work_hours": 3.5,
            "late_minutes": 15,
            "overtime_minutes": 0,
            "half_day": True,
        },
        {
            "emp_code": "EMP7701",
            "date": "2026-07-08",
            "status": "LEAVE",
            "punch_in": None,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        },
        {
            "emp_code": "EMP7701",
            "date": "2026-07-11",  # Saturday
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 11, 4, 20, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, 11, 13, 10, tzinfo=timezone.utc),
            "work_hours": 8.0,
            "late_minutes": 20,
            "overtime_minutes": 40,
            "half_day": False,
        },
        # Sales team logs for leaderboard ties
        # EMP7705: 60 late min
        {
            "emp_code": "EMP7705",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 6, 5, 0, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc),
            "work_hours": 8.0,
            "late_minutes": 60,
            "overtime_minutes": 0,
            "half_day": False,
        },
        # EMP7706: 60 late min (Tied with EMP7705!)
        {
            "emp_code": "EMP7706",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 6, 5, 0, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc),
            "work_hours": 8.0,
            "late_minutes": 60,
            "overtime_minutes": 0,
            "half_day": False,
        },
        # EMP7707: 30 late min (Rank 3 in standard competition ranking 1, 1, 3)
        {
            "emp_code": "EMP7707",
            "date": "2026-07-07",
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 7, 4, 30, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, 7, 13, 0, tzinfo=timezone.utc),
            "work_hours": 8.5,
            "late_minutes": 30,
            "overtime_minutes": 0,
            "half_day": False,
        },
    ]
    for l in logs:
        main_module.db.attendance_logs.delete_one({"emp_code": l["emp_code"], "date": l["date"]})
        main_module.db.attendance_logs.insert_one(l)


# --------------------------------------------------------------------------- #
# Endpoint 1: /analytics/employees/{emp_code}/monthly
# --------------------------------------------------------------------------- #
def test_employee_monthly_analytics():
    # EMP7701 joined 2026-07-06.
    # Total Mon-Fri in July from 6th to 31st:
    # July 2026: 31 days. July 1 (Wed), 2 (Thu), 3 (Fri) are before July 6.
    # July has 23 working days total. July 1, 2, 3 = 3 days.
    # 23 - 3 = 20 working days for EMP7701.
    res = test_client.get("/analytics/employees/EMP7701/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP7701"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 20
    # Present days: 1.0 (July 6, PRESENT Mon) + 0.5 (July 7, WFH half day Tue)
    # July 11 (Saturday) weekend record does NOT count towards present_days per R7!
    assert data["present_days"] == 1.5
    assert data["leave_days"] == 1
    # Late count: July 7 (15 min) + July 11 (20 min) = 2
    assert data["late_count"] == 2
    # Total late: 15 + 20 = 35 (weekends still contribute per R7)
    assert data["total_late_minutes"] == 35
    # Total overtime: 40 (from weekend)
    assert data["total_overtime_minutes"] == 40
    # Attendance pct: 1.5 / 20 * 100 = 7.5%
    assert data["attendance_pct"] == 7.5


def test_employee_monthly_future_joiner():
    # EMP7703 joined 2026-08-01, checking 2026-07
    res = test_client.get("/analytics/employees/EMP7703/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["working_days"] == 0
    assert data["attendance_pct"] is None


def test_employee_monthly_unknown():
    res = test_client.get("/analytics/employees/NONEXISTENT/monthly?month=2026-07")
    assert res.status_code == 404


# --------------------------------------------------------------------------- #
# Endpoint 2: /analytics/departments/summary
# --------------------------------------------------------------------------- #
def test_department_summary():
    res = test_client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    items_by_dept = {it["department"]: it for it in data["items"]}

    # Engineering: EMP7701 (joined July 6) and EMP7702 (joined Jan 1) count.
    # EMP7703 (joined Aug 1) is EXCLUDED from July headcount per R9!
    assert "Engineering" in items_by_dept
    eng = items_by_dept["Engineering"]
    assert eng["headcount"] == 2
    assert eng["present_days"] == 1.5
    assert eng["leave_count"] == 1

    # Marketing: EMP7704 has ZERO logs, but MUST still be counted in headcount per R9!
    assert "Marketing" in items_by_dept
    mkt = items_by_dept["Marketing"]
    assert mkt["headcount"] == 1
    assert mkt["present_days"] == 0.0
    assert mkt["avg_work_hours"] is None
    assert mkt["late_count"] == 0

    # Sales: EMP7705, EMP7706, EMP7707
    assert "Sales" in items_by_dept
    sales = items_by_dept["Sales"]
    assert sales["headcount"] == 3


def test_department_summary_filter():
    res = test_client.get("/analytics/departments/summary?month=2026-07&department=Marketing")
    assert res.status_code == 200
    data = res.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["department"] == "Marketing"


# --------------------------------------------------------------------------- #
# Endpoint 3: /analytics/leaderboard/late
# --------------------------------------------------------------------------- #
def test_late_leaderboard_ties():
    # In Sales department: EMP7705 (60 min) and EMP7706 (60 min) tie for rank 1.
    # Next rank is skipped to rank 3: EMP7707 (30 min).
    res = test_client.get("/analytics/leaderboard/late?month=2026-07&department=Sales&limit=2")
    assert res.status_code == 200
    data = res.json()
    items = data["items"]
    # Both tied employees must be included when limit >= 1!
    assert len(items) == 2
    assert items[0]["rank"] == 1
    assert items[1]["rank"] == 1
    assert items[0]["total_late_minutes"] == 60
    assert items[1]["total_late_minutes"] == 60
    # Deterministic secondary sort on emp_code ASC
    assert items[0]["emp_code"] == "EMP7705"
    assert items[1]["emp_code"] == "EMP7706"

    # Query with limit=3 should also include rank 3 (EMP7707)
    res_l3 = test_client.get("/analytics/leaderboard/late?month=2026-07&department=Sales&limit=3")
    assert res_l3.status_code == 200
    items_l3 = res_l3.json()["items"]
    assert len(items_l3) == 3
    assert items_l3[2]["rank"] == 3
    assert items_l3[2]["emp_code"] == "EMP7707"


# --------------------------------------------------------------------------- #
# Endpoint 4: /analytics/departments/{department}/trend
# --------------------------------------------------------------------------- #
def test_department_trend_gap_filling_and_moving_avg():
    # Query 7 days for Engineering from 2026-07-06 (Mon) to 2026-07-12 (Sun)
    res = test_client.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-12")
    assert res.status_code == 200
    data = res.json()
    assert data["department"] == "Engineering"
    items = data["items"]
    # Exactly one row per calendar day (7 days)
    assert len(items) == 7

    # Check Monday 2026-07-06:
    d1 = items[0]
    assert d1["date"] == "2026-07-06"
    assert d1["is_working_day"] is True
    # Headcount = 2 (EMP7701 joined July 6, EMP7702 joined Jan 1)
    assert d1["headcount"] == 2
    assert d1["present_count"] == 1.0
    # attendance_rate = 1.0 / 2 = 0.5000
    assert d1["attendance_rate"] == 0.5
    assert d1["moving_avg_7d"] == 0.5

    # Check Sunday 2026-07-12: weekend day, no records
    d_sun = items[6]
    assert d_sun["date"] == "2026-07-12"
    assert d_sun["is_working_day"] is False
    assert d_sun["attendance_rate"] is None  # None on weekends


def test_department_trend_validation():
    # Unknown department -> 404
    res_404 = test_client.get("/analytics/departments/NonExistentDept/trend?from=2026-07-01&to=2026-07-05")
    assert res_404.status_code == 404

    # to < from -> 422
    res_to_before = test_client.get("/analytics/departments/Engineering/trend?from=2026-07-10&to=2026-07-05")
    assert res_to_before.status_code == 422

    # range > 92 days -> 422
    res_93 = test_client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-04-15")
    assert res_93.status_code == 422


def test_analytics_parameter_validations():
    # Invalid month format (e.g. 2026-13, 2026-7)
    assert test_client.get("/analytics/employees/EMP7701/monthly?month=2026-13").status_code == 422
    assert test_client.get("/analytics/employees/EMP7701/monthly?month=2026-7").status_code == 422

    # Department summary invalid month
    assert test_client.get("/analytics/departments/summary?month=not-a-month").status_code == 422

    # Leaderboard invalid limits (limit < 1 or limit > 50)
    assert test_client.get("/analytics/leaderboard/late?month=2026-07&limit=0").status_code == 422
    assert test_client.get("/analytics/leaderboard/late?month=2026-07&limit=51").status_code == 422
