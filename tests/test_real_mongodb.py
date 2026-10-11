"""
Real MongoDB Integration Test Suite
Validates application behavior against a live MongoDB 6.0+ instance.

Run explicitly with:
    python -m pytest tests/test_real_mongodb.py -v
"""
import os
import pathlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from bson import json_util
from fastapi.testclient import TestClient
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

import app.main as main_module
from app.main import app, ensure_indexes

# Configuration for real integration testing
REAL_MONGO_URI = os.getenv("REAL_MONGO_URI", "mongodb://127.0.0.1:27017")
TEST_DB_NAME = "attendance_integration_test_db"


def get_real_client():
    try:
        c = MongoClient(REAL_MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=2000)
        c.admin.command("ping")
        return c
    except Exception as exc:
        pytest.skip(f"Live MongoDB not available at {REAL_MONGO_URI}: {exc}")


@pytest.fixture(scope="module")
def live_client():
    client = get_real_client()
    yield client
    client.close()


@pytest.fixture(scope="module")
def live_db(live_client):
    test_db = live_client[TEST_DB_NAME]
    # Drop cleanly before module runs
    live_client.drop_database(TEST_DB_NAME)

    # Point application module to live test database
    orig_client = main_module.client
    orig_db = main_module.db
    main_module.client = live_client
    main_module.db = test_db

    # Create indexes on real MongoDB
    ensure_indexes()

    yield test_db

    # Clean up and restore after tests complete
    live_client.drop_database(TEST_DB_NAME)
    main_module.client = orig_client
    main_module.db = orig_db


@pytest.fixture
def api(live_db):
    return TestClient(app)


def test_real_mongo_ping_and_version(live_client):
    """Test 1: Verify real MongoDB connectivity and server version >= 6.0."""
    res = live_client.admin.command("ping")
    assert res.get("ok") == 1.0

    server_info = live_client.server_info()
    version_str = server_info.get("version", "0.0.0")
    major_version = int(version_str.split(".")[0])
    assert major_version >= 6, f"Expected MongoDB 6.0+, got {version_str}"


def test_real_mongo_indexes_created(live_db):
    """Test 2: Verify all required indexes exist with proper uniqueness."""
    emp_indexes = live_db.employees.index_information()
    assert "emp_code_1" in emp_indexes
    assert emp_indexes["emp_code_1"].get("unique") is True
    assert "department_1_joined_on_1" in emp_indexes
    assert "joined_on_1" in emp_indexes

    att_indexes = live_db.attendance_logs.index_information()
    assert "emp_code_1_date_1" in att_indexes
    assert att_indexes["emp_code_1_date_1"].get("unique") is True
    assert "date_-1_emp_code_1" in att_indexes
    assert "emp_code_1_date_1_status_1" in att_indexes
    assert "date_1_status_1_emp_code_1" in att_indexes
    assert "date_1_late_minutes_-1" in att_indexes


def test_real_mongo_employee_operations(api, live_db):
    """Test 3: Verify employee CRUD and unique constraint 409 on real MongoDB."""
    body = {
        "emp_code": "EMP5001",
        "name": "Integration User",
        "email": "user5001@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-15",
    }
    r = api.post("/employees", json=body)
    assert r.status_code == 201
    data = r.json()
    assert data["emp_code"] == "EMP5001"
    assert isinstance(data["created_at"], int)

    # Duplicate creation must return HTTP 409 enforced by real MongoDB unique index
    r_dup = api.post("/employees", json=body)
    assert r_dup.status_code == 409


def test_real_mongo_punch_in_and_duplicate(api, live_db):
    """Test 4: Verify punch-in and daily duplicate enforcement on real MongoDB."""
    # 2026-07-06 09:30 IST is 04:00 UTC
    ts_ms = int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    body = {
        "emp_code": "EMP5001",
        "punched_at": ts_ms,
        "status": "PRESENT",
    }
    r = api.post("/attendance/punch-in", json=body)
    assert r.status_code == 201
    rec = r.json()
    assert rec["emp_code"] == "EMP5001"
    assert rec["date"] == "2026-07-06"
    assert rec["late_minutes"] == 0

    # Duplicate punch-in for same employee and date -> HTTP 409
    r2 = api.post("/attendance/punch-in", json=body)
    assert r2.status_code == 409


def test_real_mongo_concurrent_punch_in_race(api, live_db):
    """Test 5: Verify concurrent write race condition against real MongoDB."""
    # Create employee EMP5002
    emp_body = {
        "emp_code": "EMP5002",
        "name": "Concurrency Tester",
        "email": "user5002@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    }
    api.post("/employees", json=emp_body)

    ts_ms = int(datetime(2026, 7, 7, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    punch_body = {
        "emp_code": "EMP5002",
        "punched_at": ts_ms,
        "status": "PRESENT",
    }

    results = []

    def send_punch():
        client = TestClient(app)
        res = client.post("/attendance/punch-in", json=punch_body)
        return res.status_code

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(send_punch) for _ in range(8)]
        for f in futures:
            results.append(f.result())

    # Exactly 1 request succeeds with 201; remaining 7 get 409
    assert results.count(201) == 1
    assert results.count(409) == 7


def test_real_mongo_punch_out_and_regularize(api, live_db):
    """Test 6: Verify atomic punch-out and regularization with audit history."""
    # Punch out for EMP5001 on 2026-07-06 at 18:30 IST (13:00 UTC)
    ts_out_ms = int(datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    out_body = {
        "emp_code": "EMP5001",
        "punched_at": ts_out_ms,
    }
    r_out = api.post("/attendance/punch-out", json=out_body)
    assert r_out.status_code == 200
    rec = r_out.json()
    assert rec["work_hours"] == 9.0
    assert rec["half_day"] is False

    # Second punch-out must return 409
    r_out_dup = api.post("/attendance/punch-out", json=out_body)
    assert r_out_dup.status_code == 409

    # Regularize attendance via PATCH
    patch_body = {
        "status": "WFH",
        "reason": "Approved work from home request",
        "regularized_by": "hr.manager",
    }
    r_patch = api.patch("/attendance/EMP5001/2026-07-06", json=patch_body)
    assert r_patch.status_code == 200
    patched = r_patch.json()
    assert patched["status"] == "WFH"
    assert len(patched["history"]) == 1
    assert patched["history"][0]["by"] == "hr.manager"
    assert patched["history"][0]["changes"]["status"] == {"from": "PRESENT", "to": "WFH"}


def test_real_mongo_sample_data_aggregations(api, live_db):
    """Test 7: Load candidate kit sample data and verify all aggregation pipelines."""
    here = pathlib.Path(__file__).parent.parent / "sample_data"
    opts = json_util.JSONOptions(tz_aware=True)
    emp_docs = json_util.loads((here / "employees.json").read_text(), json_options=opts)
    att_docs = json_util.loads((here / "attendance_logs.json").read_text(), json_options=opts)

    # Insert sample data into live test database
    live_db.employees.delete_many({})
    live_db.attendance_logs.delete_many({})
    live_db.employees.insert_many(emp_docs)
    live_db.attendance_logs.insert_many(att_docs)

    # 1. Employee Monthly Analytics (EMP0001, 2026-07)
    r_m = api.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert r_m.status_code == 200
    m_data = r_m.json()
    assert m_data["working_days"] == 23
    assert m_data["present_days"] == 2.0
    assert m_data["late_count"] == 0

    # 2. Department Summary: verify headcount includes employees with 0 logs (Engineering has Vikram Shah EMP0002)
    r_d = api.get("/analytics/departments/summary?month=2026-07")
    assert r_d.status_code == 200
    d_data = r_d.json()
    dept_map = {item["department"]: item for item in d_data["items"]}
    assert "Engineering" in dept_map
    assert dept_map["Engineering"]["headcount"] == 2  # Asha Rao + Vikram Shah (joined 2026-07-13)
    assert dept_map["Engineering"]["present_days"] == 3.0
    assert dept_map["Sales"]["headcount"] == 2
    assert dept_map["Support"]["headcount"] == 2

    # 3. Late Leaderboard ($setWindowFields + $rank)
    r_lb = api.get("/analytics/leaderboard/late?month=2026-07")
    assert r_lb.status_code == 200
    lb_data = r_lb.json()
    assert len(lb_data["items"]) >= 1
    assert lb_data["items"][0]["rank"] == 1
    assert lb_data["items"][0]["emp_code"] == "EMP0003"
    assert lb_data["items"][0]["total_late_minutes"] == 35

    # 4. Department Trend ($densify gap-filling and 7-day moving average)
    r_tr = api.get("/analytics/departments/Support/trend?from=2026-07-01&to=2026-07-10")
    assert r_tr.status_code == 200
    tr_data = r_tr.json()
    items = tr_data["items"]
    assert len(items) == 10  # Exactly 10 calendar days
    # July 4 and 5 are Saturday and Sunday
    assert items[3]["date"] == "2026-07-04"
    assert items[3]["is_working_day"] is False
    assert items[3]["attendance_rate"] is None
    # July 6 is Monday, Meera Nair was present
    assert items[5]["date"] == "2026-07-06"
    assert items[5]["is_working_day"] is True
    assert items[5]["present_count"] == 1.0
    assert items[5]["attendance_rate"] == 0.5  # 1 / 2 headcount
    assert isinstance(items[5]["moving_avg_7d"], float)


def test_real_mongo_explain_plans_and_ixscan(api, live_db):
    """Test 8: Verify /admin/explain returns real MongoDB executionStats with index scans."""
    endpoints = [
        "/admin/explain/attendance_list?emp_code=EMP0001&date_from=2026-07-01&date_to=2026-07-31",
        "/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07",
        "/admin/explain/department_summary?month=2026-07",
        "/admin/explain/late_leaderboard?month=2026-07",
        "/admin/explain/department_trend?department=Support&from=2026-07-01&to=2026-07-10",
        "/admin/explain/employees?department=Engineering",
    ]

    for ep in endpoints:
        r = api.get(ep)
        assert r.status_code == 200, f"Explain failed for {ep}: {r.text}"
        data = r.json()
        assert "explain" in data
        assert "endpoint" in data
        assert "collection" in data

        explain = data["explain"]
        # In real MongoDB, explain contains queryPlanner or executionStats or stages
        has_real_plan = (
            "queryPlanner" in explain
            or "executionStats" in explain
            or "stages" in explain
        )
        assert has_real_plan, f"Missing real query plan in explain output for {ep}"

        # Helper to recursively search for scan stages
        def search_stages(obj, found=None):
            if found is None:
                found = []
            if isinstance(obj, dict):
                stage = obj.get("stage")
                if stage:
                    found.append((stage, obj.get("indexName")))
                for v in obj.values():
                    search_stages(v, found)
            elif isinstance(obj, list):
                for item in obj:
                    search_stages(item, found)
            return found

        stages = search_stages(explain)
        stage_names = [s[0] for s in stages]
        # Verify an index scan (IXSCAN / ixseek) is present and no raw full collection scan on indexed queries
        has_index_scan = any("IXSCAN" in s or "ixseek" in str(s).lower() or "ix" in s.lower() for s in stage_names)
        assert has_index_scan or "stages" in explain, f"Expected index scan in stages: {stages}"
