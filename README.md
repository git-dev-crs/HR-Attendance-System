# Employee Attendance & Analytics API

A FastAPI backend for HR attendance management and analytics backed by MongoDB. Implements employee lifecycle, attendance punch tracking, manual regularizations with audit logging, and analytical aggregations with index-backed performance.

## Tech Stack
- **Python**: 3.11+
- **Framework**: FastAPI + Uvicorn
- **Database**: MongoDB 7.0 (via PyMongo)
- **Validation**: Pydantic v2

---

## Getting Started

### 1. Environment & Dependencies
Create and activate a virtual environment, then install requirements:
```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# Linux / macOS:
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Configure MongoDB
Ensure MongoDB (v6.0+) is running locally on port 27017 or set the connection variables in `.env`:
```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```
*(Environment variables take precedence over `.env`)*

### 3. Seed Sample Data
```bash
python sample_seed.py
```

### 4. Run the Server
```bash
uvicorn app.main:app --port 8000 --reload
```
Interactive API documentation is available at `http://localhost:8000/docs`.

---

## Key Design & Implementation Notes

- **Startup Indexing**: All indexes are created idempotently on startup, eliminating collection scans (`COLLSCAN`) and backing all list/aggregation queries with index scans (`IXSCAN`).
- **Concurrency & Races**: Punch-in and employee creation rely on unique database constraints (`(emp_code, date)` and `emp_code`) rather than check-then-insert patterns, avoiding race conditions under simultaneous traffic.
- **Time Zone & Calendar Rules (R1–R10)**: All instants are received and returned as UTC epoch milliseconds and stored as UTC datetimes in MongoDB. Shift calculations and attendance dates evaluate in Indian Standard Time (IST, UTC+05:30), with support for overnight shifts across midnight.
- **Precision Rounding**: Work hours and rate calculations strictly follow standard half-up rounding (`Decimal(ROUND_HALF_UP)`), avoiding Python's default banker's rounding.
- **Analytics Pipelines**: Analytical endpoints use native MongoDB aggregation pipelines, including `$densify` for calendar gap-filling in daily trends and standard competition ranking (`1, 2, 2, 4`) for late-comers leaderboards.
- **Documentation**:
  - `REVIEW.md`: Detailed defect analysis and fixes identified in the starter code.
  - `DECISIONS.md`: Architectural rationale regarding indexes, concurrency, ranking, and scalability.
