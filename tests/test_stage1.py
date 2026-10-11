import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from unittest.mock import MagicMock, patch

import mongomock
import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app, datetime_to_epoch_ms, ensure_indexes

client = TestClient(app)


def is_live_mongo_available():
    try:
        main_module.client.admin.command("ping")
        return True
    except Exception:
        return False


# Determine if we should mock DB for testing
LIVE_AVAILABLE = is_live_mongo_available()


@pytest.fixture(autouse=True)
def setup_teardown_db():
    if not LIVE_AVAILABLE:
        # Use isolated mongomock client for testing
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
        main_module.db.employees.delete_many({"emp_code": {"$regex": "^EMP99"}})


def test_health_success():
    if not LIVE_AVAILABLE:
        # Mock successful ping
        mock_admin = MagicMock()
        mock_admin.command.return_value = {"ok": 1.0}
        with patch.object(main_module.client, "admin", mock_admin):
            response = client.get("/health")
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}
    else:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


def test_health_failure():
    mock_admin = MagicMock()
    mock_admin.command.side_effect = Exception("Database connection failure")
    with patch.object(main_module.client, "admin", mock_admin):
        response = client.get("/health")
        assert response.status_code == 503
        assert response.json()["detail"] == "Database ping failed"


def test_indexes_created():
    main_module.ensure_indexes()
    emp_indexes = main_module.db.employees.index_information()
    assert "emp_code_1" in emp_indexes
    assert emp_indexes["emp_code_1"].get("unique") is True


def test_create_employee_success():
    emp_code = "EMP9901"
    main_module.db.employees.delete_one({"emp_code": emp_code})

    payload = {
        "emp_code": emp_code,
        "name": "Stage One Tester",
        "email": "stage1@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-15",
    }
    response = client.post("/employees", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert data["emp_code"] == emp_code
    assert data["name"] == "Stage One Tester"
    assert data["email"] == "stage1@example.com"
    assert data["department"] == "Engineering"
    assert data["shift_start"] == "09:30"
    assert data["shift_end"] == "18:30"
    assert data["joined_on"] == "2026-01-15"
    assert isinstance(data["created_at"], int)
    assert 100000000000 <= data["created_at"] <= 4102444800000

    # Verify MongoDB document
    doc = main_module.db.employees.find_one({"emp_code": emp_code})
    assert doc is not None
    assert doc["emp_code"] == emp_code
    assert isinstance(doc["created_at"], datetime)


def test_create_employee_duplicate_409():
    emp_code = "EMP9902"
    main_module.db.employees.delete_one({"emp_code": emp_code})

    payload = {
        "emp_code": emp_code,
        "name": "First Insert",
        "email": "first@example.com",
        "department": "HR",
        "shift_start": "10:00",
        "shift_end": "19:00",
        "joined_on": "2026-02-01",
    }
    r1 = client.post("/employees", json=payload)
    assert r1.status_code == 201

    # Second insert with same emp_code
    r2 = client.post("/employees", json=payload)
    assert r2.status_code == 409
    assert f"emp_code '{emp_code}' already exists" in r2.json()["detail"]


def test_create_employee_concurrent_requests():
    emp_code = "EMP9903"
    main_module.db.employees.delete_one({"emp_code": emp_code})

    payload = {
        "emp_code": emp_code,
        "name": "Race Candidate",
        "email": "race@example.com",
        "department": "Operations",
        "shift_start": "09:00",
        "shift_end": "18:00",
        "joined_on": "2026-03-01",
    }

    results = []
    lock = threading.Lock()

    def send_req():
        res = client.post("/employees", json=payload)
        with lock:
            results.append(res.status_code)

    threads = [threading.Thread(target=send_req) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(201) == 1
    assert results.count(409) == 4


def test_create_employee_validation_errors():
    base = {
        "emp_code": "EMP9904",
        "name": "Validator",
        "email": "valid@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    }

    # Invalid emp_code format (EMP12 too short)
    p = dict(base, emp_code="EMP12")
    assert client.post("/employees", json=p).status_code == 422

    # Invalid emp_code format (not starting with EMP)
    p = dict(base, emp_code="0001EMP")
    assert client.post("/employees", json=p).status_code == 422

    # Invalid email
    p = dict(base, email="not-an-email")
    assert client.post("/employees", json=p).status_code == 422

    # Equal shift_start and shift_end
    p = dict(base, shift_start="09:30", shift_end="09:30")
    r = client.post("/employees", json=p)
    assert r.status_code == 422

    # Invalid joined_on date (e.g. 2026-02-31)
    p = dict(base, joined_on="2026-02-31")
    assert client.post("/employees", json=p).status_code == 422


def test_list_employees_pagination_and_sorting():
    test_codes = ["EMP9910", "EMP9911", "EMP9912", "EMP9913", "EMP9914"]
    for code in test_codes:
        main_module.db.employees.delete_one({"emp_code": code})
        r = client.post(
            "/employees",
            json={
                "emp_code": code,
                "name": f"Name {code}",
                "email": f"{code.lower()}@example.com",
                "department": "QA",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-01-10",
            },
        )
        assert r.status_code == 201

    # Page 1, page_size 2
    r_p1 = client.get("/employees?department=QA&page=1&page_size=2")
    assert r_p1.status_code == 200
    data_p1 = r_p1.json()
    assert data_p1["page"] == 1
    assert data_p1["page_size"] == 2
    assert data_p1["total"] == 5
    assert len(data_p1["items"]) == 2
    assert data_p1["items"][0]["emp_code"] == "EMP9910"
    assert data_p1["items"][1]["emp_code"] == "EMP9911"

    # Page 2, page_size 2
    r_p2 = client.get("/employees?department=QA&page=2&page_size=2")
    assert r_p2.status_code == 200
    data_p2 = r_p2.json()
    assert data_p2["page"] == 2
    assert data_p2["page_size"] == 2
    assert data_p2["total"] == 5
    assert len(data_p2["items"]) == 2
    assert data_p2["items"][0]["emp_code"] == "EMP9912"
    assert data_p2["items"][1]["emp_code"] == "EMP9913"

    # Page 3, page_size 2
    r_p3 = client.get("/employees?department=QA&page=3&page_size=2")
    assert r_p3.status_code == 200
    data_p3 = r_p3.json()
    assert len(data_p3["items"]) == 1
    assert data_p3["items"][0]["emp_code"] == "EMP9914"


def test_list_employees_validation_errors():
    # page < 1
    assert client.get("/employees?page=0").status_code == 422
    # page_size > 100
    assert client.get("/employees?page_size=101").status_code == 422
