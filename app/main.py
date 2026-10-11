"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real env vars win)

Layout of this file:
  1. config / client          4. request models
  2. time & rounding helpers  5. system + employees endpoints
  3. business rules R1-R5     6. attendance endpoints
                              7. analytics pipelines + endpoints
                              8. explain endpoint
"""
import calendar
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Config / client
# --------------------------------------------------------------------------- #
load_dotenv()  # override=False: real environment variables win over .env

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,  # every datetime read back is UTC-aware; never mix naive/aware
    serverSelectionTimeoutMS=5000,  # /health answers 503 quickly instead of hanging 30 s
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
log = logging.getLogger("attendance")

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
WEEKDAYS_ISO = [1, 2, 3, 4, 5]  # $isoDayOfWeek: Mon=1 .. Sun=7
DATE_RE = r"^\d{4}-\d{2}-\d{2}$"
MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"
SORT_ATTENDANCE = [("date", DESCENDING), ("emp_code", ASCENDING)]

_indexes_ready = False


def ensure_indexes() -> None:
    """Idempotent: create_index is a no-op when the same key pattern already exists."""
    global _indexes_ready
    if _indexes_ready:
        return
    emp, att = db.employees, db.attendance_logs
    emp.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    emp.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp")
    emp.create_index([("joined_on", ASCENDING)], name="joined_on")
    att.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_emp_date")
    att.create_index(SORT_ATTENDANCE, name="date_desc_emp")
    att.create_index([("status", ASCENDING)] + SORT_ATTENDANCE, name="status_date_desc_emp")
    att.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in")
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as e:  # DB not up yet: /health keeps reporting 503 and retries index creation
        log.error("index creation failed at startup: %s", e)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# 2. Time & rounding helpers
# --------------------------------------------------------------------------- #
def fail422(msg: str, loc: str = "body"):
    raise HTTPException(422, detail=[{"loc": [loc], "msg": msg, "type": "value_error"}])


def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return calendar.timegm(dt.utctimetuple()) * 1000 + dt.microsecond // 1000


def now_utc() -> datetime:
    """Current time at BSON (millisecond) precision, so stored == returned."""
    n = datetime.now(UTC)
    return n.replace(microsecond=n.microsecond // 1000 * 1000)


def parse_date(s: str, loc: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        fail422("invalid date, expected YYYY-MM-DD", loc)


def hhmm(s: str) -> time:
    h, m = map(int, s.split(":"))
    return time(h, m)


def half_up(x, places: int) -> float:
    return float(Decimal(x).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def mongo_half_up(expr, places: int):
    """Half-up rounding inside a pipeline. $round is half-even, so do it by hand on Decimal128."""
    f = 10**places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [{"$toDecimal": expr}, f]}, 0.5]}}, f]}


def month_range(month: str) -> tuple[str, str]:
    y, m = int(month[:4]), int(month[5:])
    return f"{month}-01", f"{month}-{calendar.monthrange(y, m)[1]:02d}"


def working_days(start: date, end: date) -> int:
    return sum(1 for i in range((end - start).days + 1) if (start + timedelta(i)).weekday() < 5)


# --------------------------------------------------------------------------- #
# 3. Business rules R1-R5
# --------------------------------------------------------------------------- #
def is_overnight(emp: dict) -> bool:
    return emp["shift_end"] <= emp["shift_start"]


def attendance_date(punch_in: datetime, emp: dict) -> date:
    """R1: IST calendar date of punch_in; overnight shift punch-ins before shift_end belong to the previous day."""
    local = punch_in.astimezone(IST)
    d = local.date()
    if is_overnight(emp) and local.time() < hhmm(emp["shift_end"]):
        d -= timedelta(days=1)
    return d


def shift_bounds(emp: dict, day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, hhmm(emp["shift_start"]), IST)
    end = datetime.combine(day + timedelta(days=1 if is_overnight(emp) else 0), hhmm(emp["shift_end"]), IST)
    return start, end


def derive(emp: dict, day: date, punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    """R2-R5 from the final punch times. ABSENT/LEAVE (no punch_in) -> defaults."""
    out = {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    if punch_in is None:
        return out
    start, end = shift_bounds(emp, day)
    late_secs = int((punch_in - start).total_seconds())
    if late_secs > 600:  # strictly more than 10:00 after shift start
        out["late_minutes"] = late_secs // 60  # floor since shift_start, not since grace end
    if punch_out is not None:
        wh = (Decimal(int((punch_out - punch_in).total_seconds())) / 3600).quantize(Decimal("0.01"), ROUND_HALF_UP)
        out["work_hours"] = float(wh)
        out["half_day"] = wh < Decimal("4.50")
        ot = int((punch_out - end).total_seconds()) // 60
        out["overtime_minutes"] = ot if ot >= 30 else 0
    return out


# --------------------------------------------------------------------------- #
# 4. Request models
# --------------------------------------------------------------------------- #
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
HHMM = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]


class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: str = Field(pattern=DATE_RE)

    @model_validator(mode="after")
    def _check(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        date.fromisoformat(self.joined_on)
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeRequest(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _no_explicit_null(self):
        for f in ("status", "punch_in", "punch_out"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} may not be null")
        return self


# --------------------------------------------------------------------------- #
# Serialisation (Mongo doc -> API shape)
# --------------------------------------------------------------------------- #
def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"], "name": doc["name"], "email": doc["email"], "department": doc["department"],
        "shift_start": doc["shift_start"], "shift_end": doc["shift_end"], "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def history_out(entries: list) -> list:
    out = []
    for e in entries:
        changes = {}
        for k, v in e.get("changes", {}).items():
            conv = dt_to_ms if k in ("punch_in", "punch_out") else (lambda x: x)
            changes[k] = {"from": conv(v.get("from")), "to": conv(v.get("to"))}
        out.append({"at": dt_to_ms(e.get("at")), "by": e.get("by"), "reason": e.get("reason"), "changes": changes})
    return out


def record_out(doc: dict) -> dict:
    """Legacy seeded docs may lack half_day / history / counters: missing means false / [] / 0."""
    return {
        "emp_code": doc["emp_code"], "date": doc["date"], "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")), "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"), "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0, "half_day": bool(doc.get("half_day", False)),
        "history": history_out(doc.get("history") or []),
    }


def get_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 5. System + employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "mongodb unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # uniqueness enforced by the index, not by check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department else {}
    cur = db.employees.find(q).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cur], "total": db.employees.count_documents(q),
            "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# 6. Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = get_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at else now_utc().replace(microsecond=0)
    day = attendance_date(ts, emp)
    doc = {"emp_code": emp["emp_code"], "date": day.isoformat(), "status": body.status,
           "punch_in": ts, "punch_out": None, **derive(emp, day, ts, None), "history": []}
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): the race loser gets DuplicateKeyError
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = get_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at else now_utc().replace(microsecond=0)
    rec = db.attendance_logs.find_one({"emp_code": emp["emp_code"], "punch_in": {"$lte": ts}},
                                      sort=[("punch_in", DESCENDING)])
    if not rec:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    delta = ts - rec["punch_in"]
    if delta <= timedelta(0) or delta > timedelta(hours=24):
        fail422("punched_at must be after punch_in and within 24 hours", "punched_at")
    fields = {"punch_out": ts, **derive(emp, date.fromisoformat(rec["date"]), rec["punch_in"], ts)}
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None}, {"$set": fields}, return_document=ReturnDocument.AFTER
    )
    if not updated:  # a concurrent punch-out won the race
        raise HTTPException(409, "already punched out")
    return record_out(updated)


def attendance_query(emp_code, date_from, date_to, status) -> dict:
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        f = parse_date(date_from, "date_from") if date_from else None
        t = parse_date(date_to, "date_to") if date_to else None
        if f and t and f > t:
            fail422("date_from must be <= date_to", "date_from")
        q["date"] = {k: v for k, v in (("$gte", date_from), ("$lte", date_to)) if v}
    if status:
        q["status"] = status
    return q


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=DATE_RE),
    date_to: Optional[str] = Query(None, pattern=DATE_RE),
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = attendance_query(emp_code, date_from, date_to, status)
    cur = db.attendance_logs.find(q).sort(SORT_ATTENDANCE).skip((page - 1) * page_size).limit(page_size)
    return {"items": [record_out(d) for d in cur], "total": db.attendance_logs.count_documents(q),
            "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeRequest):
    day = parse_date(date, "date")
    emp = get_employee(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "no attendance record for that date")

    status = body.status or rec["status"]
    if status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            fail422(f"{status} cannot carry punch times", "status")
        pin = pout = None
    else:
        pin = ms_to_dt(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
        pout = ms_to_dt(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
        if pin is None:
            fail422(f"{status} requires a punch_in", "punch_in")
        if attendance_date(pin, emp) != day:
            fail422("punch_in must fall on the record's attendance date", "punch_in")
        if pout is not None and not (timedelta(0) < pout - pin <= timedelta(hours=24)):
            fail422("punch_out must be after punch_in and within 24 hours", "punch_out")

    new = {"status": status, "punch_in": pin, "punch_out": pout, **derive(emp, day, pin, pout)}
    old = {"status": rec["status"], "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out"),
           "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
           "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day", False))}
    changes = {k: {"from": old[k], "to": new[k]} for k in new if old[k] != new[k]}
    if not changes:
        fail422("request changes nothing")

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    n_hist = len(rec.get("history") or [])
    updated = db.attendance_logs.find_one_and_update(
        # optimistic lock on the history length: a concurrent correction that landed first makes this miss
        {"_id": rec["_id"], "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, n_hist]}},
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "record was modified concurrently, retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# 7. Analytics (aggregation pipelines)
# --------------------------------------------------------------------------- #
IS_PRESENCE = {"$in": ["$status", PRESENCE]}
IS_LATE = {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}
DAY_VALUE = {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}
IS_WEEKDAY = {"$in": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}},
                      WEEKDAYS_ISO]}
PRESENT_DAY = {"$cond": [{"$and": [IS_PRESENCE, IS_WEEKDAY]}, DAY_VALUE, 0]}  # R5-R7


def monthly_pipeline(emp_code: str, month: str) -> list:
    start, end = month_range(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": PRESENT_DAY},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [IS_LATE, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def summary_pipeline(month: str, department: Optional[str]) -> list:
    start, end = month_range(month)
    match = {"joined_on": {"$lte": end}}  # R9: joined by month end, with or without logs
    if department:
        match["department"] = department
    has_hours = {"$and": [IS_PRESENCE, {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]}
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "s",
            "pipeline": [
                {"$match": {"date": {"$gte": start, "$lte": end}}},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": PRESENT_DAY},
                    "wh_sum": {"$sum": {"$cond": [has_hours, {"$toDecimal": "$work_hours"}, 0]}},
                    "wh_n": {"$sum": {"$cond": [has_hours, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [IS_LATE, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                }},
            ],
        }},
        {"$unwind": {"path": "$s", "preserveNullAndEmptyArrays": True}},  # keep employees with zero logs
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$ifNull": ["$s.present_days", 0]}},
            "wh_sum": {"$sum": {"$ifNull": ["$s.wh_sum", 0]}},
            "wh_n": {"$sum": {"$ifNull": ["$s.wh_n", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$s.late_count", 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$s.total_late_minutes", 0]}},
            "leave_count": {"$sum": {"$ifNull": ["$s.leave_count", 0]}},
            "on_duty_count": {"$sum": {"$ifNull": ["$s.on_duty_count", 0]}},
        }},
        {"$project": {
            "_id": 0, "department": "$_id", "headcount": 1, "present_days": 1, "late_count": 1,
            "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
            "avg_work_hours": {"$cond": [
                {"$gt": ["$wh_n", 0]},
                {"$toDouble": mongo_half_up({"$divide": ["$wh_sum", "$wh_n"]}, 2)},  # mean over records
                None,
            ]},
        }},
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    start, end = month_range(month)
    emp_match = {"department": department} if department else {}
    return [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp",
                     "pipeline": [{"$match": emp_match}, {"$project": {"_id": 0, "name": 1, "department": 1}}]}},
        {"$unwind": "$emp"},  # drops logs whose employee is missing / outside the department
        # standard competition ranking: ties share a rank, next rank skipped (1, 2, 2, 4)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies after ranking, so ties at the cutoff stay
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]


def trend_pipeline(department: str, d_from: date, d_to: date) -> list:
    lo = datetime.combine(d_from, time(), UTC)
    hi = datetime.combine(d_to + timedelta(days=1), time(), UTC)  # $densify bounds are [lo, hi)
    day_str = {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}}
    return [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "logs",
            "pipeline": [{"$match": {"date": {"$gte": d_from.isoformat(), "$lte": d_to.isoformat()}}},
                         {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}}],
        }},
        {"$unwind": "$logs"},
        {"$replaceWith": "$logs"},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [IS_PRESENCE, DAY_VALUE, 0]}},  # weekends included here
            "late_count": {"$sum": {"$cond": [IS_LATE, 1, 0]}},
        }},
        {"$project": {"_id": 0, "present_count": 1, "late_count": 1,
                      "day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        # gap filling: exactly one row per calendar day in [from, to]
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [lo, hi]}}},
        {"$sort": {"day": 1}},
        {"$lookup": {  # R9 headcount as of that day
            "from": "employees", "as": "hc", "let": {"d": day_str},
            "pipeline": [{"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$d"]}}},
                         {"$count": "n"}],
        }},
        {"$set": {
            "date": day_str,
            "is_working_day": {"$in": [{"$isoDayOfWeek": "$day"}, WEEKDAYS_ISO]},
            "headcount": {"$ifNull": [{"$first": "$hc.n"}, 0]},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            mongo_half_up({"$divide": ["$present_count", "$headcount"]}, 4),
            None,
        ]}}},
        # moving average over the current row and up to 6 preceding rows inside the range; $avg skips nulls
        {"$setWindowFields": {"sortBy": {"day": 1},
                              "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
                      "attendance_rate": {"$toDouble": "$attendance_rate"},
                      "moving_avg_7d": {"$toDouble": mongo_half_up("$moving_avg_7d", 4)}}},
    ]


MonthParam = Query(..., pattern=MONTH_RE)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = MonthParam):
    emp = get_employee(emp_code)
    start, end = month_range(month)
    agg = next(iter(db.attendance_logs.aggregate(monthly_pipeline(emp_code, month))), {})
    first = max(date.fromisoformat(start), date.fromisoformat(emp["joined_on"]))  # R7: from joined_on
    wd = working_days(first, date.fromisoformat(end))
    present = agg.get("present_days", 0)
    return {
        "emp_code": emp_code, "month": month, "working_days": wd, "present_days": present,
        "leave_days": agg.get("leave_days", 0), "late_count": agg.get("late_count", 0),
        "total_late_minutes": agg.get("total_late_minutes", 0),
        "total_overtime_minutes": agg.get("total_overtime_minutes", 0),
        "attendance_pct": half_up(Decimal(str(present)) / wd * 100, 2) if wd else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = MonthParam, department: Optional[str] = None):
    return {"month": month, "items": list(db.employees.aggregate(summary_pipeline(month, department)))}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = MonthParam, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    return {"month": month, "items": list(db.attendance_logs.aggregate(leaderboard_pipeline(month, limit, department)))}


def trend_range(d_from: str, d_to: str) -> tuple[date, date]:
    f, t = parse_date(d_from, "from"), parse_date(d_to, "to")
    if t < f:
        fail422("to must be >= from", "to")
    if (t - f).days + 1 > 92:
        fail422("range exceeds 92 days", "to")
    return f, t


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, d_from: str = Query(..., alias="from", pattern=DATE_RE),
                     d_to: str = Query(..., alias="to", pattern=DATE_RE)):
    f, t = trend_range(d_from, d_to)
    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(404, "department not found")
    return {"department": department, "items": list(db.employees.aggregate(trend_pipeline(department, f, t)))}


# --------------------------------------------------------------------------- #
# 8. Explain
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=DATE_RE),
    date_to: Optional[str] = Query(None, pattern=DATE_RE),
    status: Optional[Status] = None,
    d_from: Optional[str] = Query(None, alias="from", pattern=DATE_RE),
    d_to: Optional[str] = Query(None, alias="to", pattern=DATE_RE),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    def need(**params):
        for k, v in params.items():
            if v is None:
                fail422(f"{k} is required for {endpoint}", k)

    if endpoint == "attendance_list":
        coll = "attendance_logs"
        cmd = {"find": coll, "filter": attendance_query(emp_code, date_from, date_to, status),
               "sort": dict(SORT_ATTENDANCE), "skip": (page - 1) * page_size, "limit": page_size}
    else:
        if endpoint == "employee_monthly":
            need(emp_code=emp_code, month=month)
            coll, pipeline = "attendance_logs", monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            need(month=month)
            coll, pipeline = "employees", summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            need(month=month)
            coll, pipeline = "attendance_logs", leaderboard_pipeline(month, limit, department)
        else:
            need(department=department, **{"from": d_from, "to": d_to})
            coll, pipeline = "employees", trend_pipeline(department, *trend_range(d_from, d_to))
        cmd = {"aggregate": coll, "pipeline": pipeline, "cursor": {}}
    raw = db.command({"explain": cmd, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": coll, "explain": json.loads(json_util.dumps(raw))}
