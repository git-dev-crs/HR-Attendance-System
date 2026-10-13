"""
Employee Attendance & Analytics API

Run: uvicorn app.main:app --port 8000
Env: MONGO_URI, MONGO_DB
"""
import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field, model_validator
import pymongo
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

# Real environment variables take precedence over .env
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create indexes idempotently at startup
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1)])
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("joined_on", 1)])

    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", -1)])
    db.attendance_logs.create_index([("date", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers & Business Rules (R1 - R10)
# --------------------------------------------------------------------------- #
def round_half_up(val: float, decimals: int) -> float:
    """R8 Rounding: Half-up rounding using Decimal."""
    d = Decimal(f"{val:.10f}")
    fmt = "0." + "0" * decimals if decimals > 0 else "0"
    return float(d.quantize(Decimal(fmt), rounding=ROUND_HALF_UP))


def truncate_seconds(dt: datetime) -> datetime:
    """R1: Before any calculation, instants are truncated to whole seconds."""
    return dt.replace(microsecond=0)


def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def from_epoch_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(microsecond=0)


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start


def get_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1 Time zone and attendance date.
    Shift times and attendance dates are IST (UTC+05:30).
    For overnight shifts (shift_end <= shift_start): a punch-in whose IST time
    is earlier than shift_end belongs to the shift that started the previous day.
    """
    dt_ist = punch_in_dt.astimezone(IST)
    if is_overnight_shift(shift_start, shift_end):
        end_h, end_m = map(int, shift_end.split(":"))
        if (dt_ist.hour, dt_ist.minute, dt_ist.second) < (end_h, end_m, 0):
            return (dt_ist.date() - timedelta(days=1)).isoformat()
    return dt_ist.date().isoformat()


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, attendance_date_str: str) -> int:
    """
    R2 Late. Grace period is 10 minutes. A punch-in is late only if strictly more
    than 10:00 after shift_start. When late, late_minutes = whole minutes (floor)
    elapsed since shift_start. Otherwise 0.
    """
    att_d = date.fromisoformat(attendance_date_str)
    sh, sm = map(int, shift_start.split(":"))
    shift_start_dt = datetime(att_d.year, att_d.month, att_d.day, sh, sm, 0, tzinfo=IST)
    punch_in_ist = punch_in_dt.astimezone(IST)
    diff_sec = (punch_in_ist - shift_start_dt).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_overtime_minutes(punch_out_dt: datetime, shift_start: str, shift_end: str, attendance_date_str: str) -> int:
    """
    R3 Overtime. overtime_minutes = whole minutes (floor) between shift_end and
    punch_out, counted only when that is >= 30; otherwise 0.
    Shift end of an overnight shift falls on the next calendar day.
    """
    att_d = date.fromisoformat(attendance_date_str)
    eh, em = map(int, shift_end.split(":"))
    if is_overnight_shift(shift_start, shift_end):
        shift_end_date = att_d + timedelta(days=1)
    else:
        shift_end_date = att_d
    shift_end_dt = datetime(shift_end_date.year, shift_end_date.month, shift_end_date.day, eh, em, 0, tzinfo=IST)
    punch_out_ist = punch_out_dt.astimezone(IST)
    diff_sec = (punch_out_ist - shift_end_dt).total_seconds()
    diff_min = int(diff_sec // 60)
    if diff_min >= 30:
        return diff_min
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """
    R4 Work hours. (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    return round_half_up(diff_sec / 3600.0, 2)


def get_month_days_range(month_str: str):
    y, m = map(int, month_str.split("-"))
    last_day = calendar.monthrange(y, m)[1]
    return f"{month_str}-01", f"{month_str}-{last_day:02d}", last_day


def get_working_days_count(month_str: str, joined_on_str: str) -> int:
    """
    R7 Working days. Mon-Fri, counted from joined_on for mid-month joiners.
    """
    y, m = map(int, month_str.split("-"))
    last_day = calendar.monthrange(y, m)[1]
    month_start = date(y, m, 1)
    month_end = date(y, m, last_day)
    joined_d = date.fromisoformat(joined_on_str)
    if joined_d > month_end:
        return 0
    start_d = max(month_start, joined_d)
    count = 0
    cur = start_d
    while cur <= month_end:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)
    return count


def format_attendance_record(doc: dict) -> dict:
    history = []
    for h in (doc.get("history") or []):
        formatted_changes = {}
        for k, v in (h.get("changes") or {}).items():
            from_val = v.get("from")
            to_val = v.get("to")
            if k in ("punch_in", "punch_out"):
                if isinstance(from_val, datetime):
                    from_val = to_epoch_ms(from_val)
                if isinstance(to_val, datetime):
                    to_val = to_epoch_ms(to_val)
            formatted_changes[k] = {"from": from_val, "to": to_val}

        at_val = h.get("at")
        if isinstance(at_val, datetime):
            at_val = to_epoch_ms(at_val)

        history.append({
            "at": at_val,
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": formatted_changes
        })

    return {
        "emp_code": doc.get("emp_code"),
        "date": doc.get("date"),
        "status": doc.get("status"),
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0) or 0,
        "overtime_minutes": doc.get("overtime_minutes", 0) or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": history
    }


def format_employee_record(doc: dict) -> dict:
    created_at = doc.get("created_at")
    if isinstance(created_at, datetime):
        created_at = to_epoch_ms(created_at)
    return {
        "emp_code": doc.get("emp_code"),
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": created_at
    }


# --------------------------------------------------------------------------- #
# Request & Response Models
# --------------------------------------------------------------------------- #
EpochMillis = Annotated[int, Field(strict=True, ge=100000000000, le=4102444800000)]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
AllStatus = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @model_validator(mode="after")
    def validate_rules(self):
        try:
            date.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("joined_on must be a valid date in YYYY-MM-DD format")
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeRequest(BaseModel):
    status: Optional[AllStatus] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.get("/health", tags=["system"])
def health():
    """Readiness probe. Returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unreachable")


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """
    Create an employee.
    Enforces uniqueness by database unique index (concurrent duplicates return 409).
    """
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": now_utc
    }
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return format_employee_record(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20
):
    """
    List employees sorted by emp_code ascending.
    Total reflects the applied filters.
    """
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items_cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [format_employee_record(doc) for doc in items_cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """
    Record a punch-in. Creates the day's record.
    Follows R1 (date determination and truncation) and R2 (late calculation).
    Concurrent duplicate requests for the same attendance date yield 409.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_dt = from_epoch_ms(body.punched_at)
    else:
        punch_in_dt = datetime.now(timezone.utc).replace(microsecond=0)

    att_date = get_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_dt, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return format_attendance_record(doc)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[AllStatus] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20
):
    """
    List attendance records sorted by date descending, emp_code ascending.
    Uses index scan on ~100k records without full loading into memory.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

    q: Dict[str, Any] = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    items_cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [format_attendance_record(doc) for doc in items_cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


