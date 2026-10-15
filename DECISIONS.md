# DECISIONS.md

1. **Indexes.**
   - `employees`:
     - `{ emp_code: 1 }` (unique): Enforces employee uniqueness (409) and fast lookups.
     - `{ department: 1, joined_on: 1 }`: Covers department filtering and date checks for headcount.
     - `{ joined_on: 1 }`: Supports company-wide headcount calculations.
   - `attendance_logs`:
     - `{ emp_code: 1, date: 1 }` (unique): Enforces the one-record-per-day rule and eliminates punch-in races.
     - `{ date: -1, emp_code: 1 }`: Powers paginated `GET /attendance` without in-memory sorting.
     - `{ emp_code: 1, punch_in: -1 }`: Powers `POST /attendance/punch-out` to find the latest punch-in.
     - `{ date: 1, late_minutes: 1 }`: Enables an index-only scan (`IXSCAN`) for late leaderboard analytics.
   - *Considered & rejected:* Storing and indexing `department` in `attendance_logs`. Denormalizing department would duplicate data and complicate updates on employee transfer; normalized lookups keep writes clean.

2. **Punch-in race.**
   Two concurrent punch-in requests calculate the same attendance date. Rather than check-then-insert, both issue `insert_one()`. MongoDB's unique index on `(emp_code, date)` guarantees atomicity: one succeeds (201), the other raises `DuplicateKeyError`, caught and converted to 409.

3. **Ties.**
   We assign standard competition ranks (e.g., 1, 2, 2, 4) where tied employees share a rank. We filter by `rank <= limit` after ranking. If employees tie at rank 10, all are returned, which can result in more than `limit` rows as specified.

4. **Headcount.**
   Headcount is computed by querying `employees` where `joined_on <= end_of_month` grouped by department. This includes employees with zero logs in the month.

5. **One thing you would change at 100x data.**
   Pre-aggregate daily and monthly department analytics via change streams or asynchronous event workers into materialized rollup collections, eliminating repetitive aggregations across 100k+ punch logs.
