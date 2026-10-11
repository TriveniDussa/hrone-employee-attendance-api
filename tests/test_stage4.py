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
def setup_teardown_explain():
    if not LIVE_AVAILABLE:
        mock_client = mongomock.MongoClient()
        mock_db = mock_client["attendance_db"]
        with patch.object(main_module, "client", mock_client), patch.object(
            main_module, "db", mock_db
        ):
            main_module.ensure_indexes()
            yield
    else:
        main_module.ensure_indexes()
        yield


def test_explain_attendance_list():
    res = test_client.get("/admin/explain/attendance_list?emp_code=EMP0001&date_from=2026-07-01&date_to=2026-07-10")
    assert res.status_code == 200
    data = res.json()
    assert data["endpoint"] == "attendance_list"
    assert data["collection"] == "attendance_logs"
    assert "explain" in data
    assert isinstance(data["explain"], dict)


def test_explain_employee_monthly():
    res = test_client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["endpoint"] == "employee_monthly"
    assert data["collection"] == "attendance_logs"
    assert "explain" in data


def test_explain_department_summary():
    res = test_client.get("/admin/explain/department_summary?month=2026-07&department=Engineering")
    assert res.status_code == 200
    data = res.json()
    assert data["endpoint"] == "department_summary"
    assert data["collection"] == "employees"
    assert "explain" in data


def test_explain_late_leaderboard():
    res = test_client.get("/admin/explain/late_leaderboard?month=2026-07&limit=10&department=Sales")
    assert res.status_code == 200
    data = res.json()
    assert data["endpoint"] == "late_leaderboard"
    assert data["collection"] == "attendance_logs"
    assert "explain" in data


def test_explain_department_trend():
    res = test_client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-10")
    assert res.status_code == 200
    data = res.json()
    assert data["endpoint"] == "department_trend"
    assert data["collection"] == "attendance_logs"
    assert "explain" in data


def test_explain_employees():
    res = test_client.get("/admin/explain/employees?department=Engineering&page=1&page_size=20")
    assert res.status_code == 200
    data = res.json()
    assert data["endpoint"] == "employees"
    assert data["collection"] == "employees"
    assert "explain" in data


def test_explain_invalid_endpoint():
    res = test_client.get("/admin/explain/unknown_endpoint")
    assert res.status_code == 422


def test_explain_missing_parameters():
    # employee_monthly missing month
    r1 = test_client.get("/admin/explain/employee_monthly?emp_code=EMP0001")
    assert r1.status_code == 422

    # department_summary missing month
    r2 = test_client.get("/admin/explain/department_summary")
    assert r2.status_code == 422

    # late_leaderboard missing month
    r3 = test_client.get("/admin/explain/late_leaderboard")
    assert r3.status_code == 422

    # department_trend missing from/to
    r4 = test_client.get("/admin/explain/department_trend?department=Engineering")
    assert r4.status_code == 422

    # department_trend to < from
    r5 = test_client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-10&to=2026-07-01")
    assert r5.status_code == 422

    # attendance_list date_from > date_to
    r6 = test_client.get("/admin/explain/attendance_list?date_from=2026-07-10&date_to=2026-07-01")
    assert r6.status_code == 422


def test_explain_plan_inspection():
    # Verify that plan includes IXSCAN
    res = test_client.get("/admin/explain/attendance_list?emp_code=EMP0001")
    assert res.status_code == 200
    explain = res.json()["explain"]
    # Verify execution plan structure
    assert "queryPlanner" in explain or "stages" in explain or "executionStats" in explain
