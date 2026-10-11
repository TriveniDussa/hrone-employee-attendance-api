# Employee Attendance & Analytics API

A high-performance FastAPI and MongoDB service implementing attendance tracking, manual regularization with audit trails, aggregation-driven analytics, and query explain inspection in compliance with the OpenAPI 3.0.3 specification.

## Features Implemented

* **System & Employee Management:**
  * Health readiness probe verifying MongoDB connection via admin ping.
  * Employee creation with strict schema constraints, regex validation, shift validations, and concurrency-safe uniqueness enforcement.
  * Deterministic listing with database-level pagination (`(page - 1) * page_size`) and department filtering.
* **Attendance Operations:**
  * Instant truncation to whole seconds and timezone handling in IST (`Asia/Kolkata`, UTC+05:30).
  * Date assignment with overnight shift boundary handling (R1).
  * Accurate late calculation with a 10-minute grace period evaluated strictly from shift start (R2).
  * Concurrency-safe punch-in enforcing single daily entries via compound unique indexes.
  * Atomic punch-out with overtime (≥ 30 min, R3), work hours rounded via `Decimal(ROUND_HALF_UP)` (R4), and half-day detection (< 4.50 hrs, R5).
  * Attendance listing paginated and sorted entirely within MongoDB.
  * Manual regularization (`PATCH /attendance/{emp_code}/{date}`) with automatic field recomputation, change-detection validation, and optimistic concurrency-safe audit trail appending.
* **Analytics Aggregation Pipelines:**
  * **Employee Monthly Summary:** Working day calculation based on mid-month joining dates (Mon–Fri), present days (half day = 0.5), leave counts, and overtime.
  * **Department Monthly Summary:** Cross-collection aggregation including employees with zero attendance logs in headcount.
  * **Late Leaderboard:** Standard competition ranking (1, 2, 2, 4) using `$setWindowFields` and `$rank`, applying limits post-ranking to preserve ties.
  * **Department Daily Trend:** Gap-filling daily series using `$densify` with 7-day rolling window moving average.
* **Admin Explain Probe (`GET /admin/explain/{endpoint}`):**
  * Runs MongoDB `explain("executionStats")` on each query or aggregation pipeline matching the exact filters, sorts, and limits of the real endpoint.
  * Supported endpoints: `attendance_list`, `employee_monthly`, `department_summary`, `late_leaderboard`, `department_trend`, and `employees`.
  * All underlying collections utilize covered compound indexes ensuring `IXSCAN` plans without `COLLSCAN`.

## Requirements

* Python 3.10+
* MongoDB 6.0+ (the grading environment uses MongoDB 7.0)

## Quick Start

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure Environment:**
   Set `MONGO_URI` and `MONGO_DB` in your environment or a local `.env` file:
   ```bash
   MONGO_URI=mongodb://localhost:27017
   MONGO_DB=attendance_db
   ```

3. **Run the API:**
   ```bash
   uvicorn app.main:app --port 8000
   ```

4. **Run the Automated Test Suite:**
   ```bash
   python -m pytest tests/ -v
   ```

5. **Run the Real MongoDB Integration Suite:**
   ```bash
   python -m pytest tests/test_real_mongodb.py -v
   ```
