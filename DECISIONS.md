# DECISIONS.md

1. **Indexes.**
   On `employees`, we created `{"emp_code": 1}` (unique) to enforce identity uniqueness and accelerate point lookups; `{"department": 1, "joined_on": 1}` to optimize department headcount filtering; and `{"joined_on": 1}` to avoid collection scans when `department` is omitted in department summary aggregations. On `attendance_logs`, we created `{"emp_code": 1, "date": 1}` (unique) to prevent duplicate daily records and race conditions; `{"date": -1, "emp_code": 1}` to cover sorting and pagination without memory overhead; `{"emp_code": 1, "date": 1, "status": 1}` and `{"date": 1, "status": 1, "emp_code": 1}` for filtered aggregation stages; and `{"date": 1, "late_minutes": -1}` to power the late leaderboard. In `department_trend`, we optimized the pipeline to begin with an initial `$match` on date range, ensuring an immediate `IXSCAN` rather than scanning raw logs. We considered and rejected a standalone index on `status` due to low cardinality.

2. **Punch-in race.**
   Both concurrent requests validate inputs and execute `insert_one`. MongoDB's unique compound index on `(emp_code, date)` enforces atomic constraint validation at the engine level: exactly one request succeeds and receives HTTP 201; the competing request raises `DuplicateKeyError`, which is caught to return HTTP 409.

3. **Ties.**
   Using MongoDB `$rank` in `$setWindowFields`, we apply standard competition ranking (1, 2, 2, 4). When employees tie at `limit`, they share the same rank. Because filtering (`rank <= limit`) occurs after ranking, both tied employees are returned, sorted deterministically by `total_late_minutes` DESC, then `emp_code` ASC.

4. **Headcount.**
   The aggregation begins on the `employees` collection matching `joined_on <= last_day_of_month` and groups by department, establishing authoritative headcount before performing a `$lookup` into `attendance_logs`. Employees with zero logs are retained in the headcount.

5. **One thing you would change for 100x data.**
   Implement incremental rollup documents (materialized daily/monthly attendance summaries updated on punch events) so analytics queries perform single-document index reads instead of scanning millions of raw log entries.
