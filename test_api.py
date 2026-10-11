"""
End-to-end self-check. Starts the app on a scratch database, seeds awkward cases, fires real HTTP requests.

    python test_api.py            (needs MONGO_URI; uses database MONGO_TEST_DB, default attendance_test)

Covers: R1-R10, status codes, races (punch-in / punch-out / regularize / duplicate employee), ties, weekend
records, mid-month joiner, no-log employee, overnight shift, gap filling and explain plans.
"""
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import httpx
from bson import json_util
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DBNAME = os.getenv("MONGO_TEST_DB", "attendance_test")
BASE = "http://127.0.0.1:8765"


def ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def d(s: str) -> dict:
    return {"$date": s}


EMPLOYEES = [
    dict(emp_code="EMP0001", name="Asha Rao", email="asha@example.com", department="Engineering", shift_start="09:30", shift_end="18:30", joined_on="2026-01-05", created_at=d("2026-01-05T04:30:00Z")),
    dict(emp_code="EMP0002", name="Vikram Shah", email="vikram@example.com", department="Engineering", shift_start="09:30", shift_end="18:30", joined_on="2026-07-13", created_at=d("2026-07-13T04:00:00Z")),
    dict(emp_code="EMP0003", name="Neha Iyer", email="neha@example.com", department="Sales", shift_start="09:30", shift_end="18:30", joined_on="2026-02-01", created_at=d("2026-02-01T04:30:00Z")),
    dict(emp_code="EMP0004", name="Rohit Das", email="rohit@example.com", department="Sales", shift_start="09:30", shift_end="18:30", joined_on="2026-02-01", created_at=d("2026-02-01T04:31:00Z")),
    dict(emp_code="EMP0005", name="Meera Nair", email="meera@example.com", department="Support", shift_start="22:00", shift_end="06:00", joined_on="2026-03-10", created_at=d("2026-03-10T04:30:00Z")),
    dict(emp_code="EMP0006", name="Karan Bedi", email="karan@example.com", department="Support", shift_start="09:30", shift_end="18:30", joined_on="2026-03-10", created_at=d("2026-03-10T04:31:00Z")),
    dict(emp_code="EMP0007", name="Future Joiner", email="future@example.com", department="Engineering", shift_start="09:30", shift_end="18:30", joined_on="2026-08-01", created_at=d("2026-08-01T04:31:00Z")),
]


def log(code, day, status, pin, pout, wh, late, ot, half=False, history=None):
    doc = dict(emp_code=code, date=day, status=status, punch_in=d(pin) if pin else None, punch_out=d(pout) if pout else None,
               work_hours=wh, late_minutes=late, overtime_minutes=ot)
    if half is not None:
        doc["half_day"] = half
    if history is not None:
        doc["history"] = history
    return doc


# July 2026: Jul 4/11 = Sat, Jul 6/13 = Mon. IST = UTC+5:30.
LOGS = [
    log("EMP0001", "2026-07-06", "PRESENT", "2026-07-06T03:58:00Z", "2026-07-06T13:05:00Z", 9.12, 0, 0, history=[]),
    log("EMP0003", "2026-07-06", "PRESENT", "2026-07-06T04:35:00Z", "2026-07-06T13:40:00Z", 9.08, 35, 40, history=[]),
    log("EMP0006", "2026-07-07", "PRESENT", "2026-07-07T04:10:01Z", None, None, 10, 0, history=[]),          # open record
    log("EMP0004", "2026-07-07", "WFH", "2026-07-07T04:00:00Z", "2026-07-07T07:30:00Z", 3.5, 0, 0, True, []),  # half day
    log("EMP0004", "2026-07-08", "ABSENT", None, None, None, 0, 0, history=[]),
    log("EMP0003", "2026-07-08", "LEAVE", None, None, None, 0, 0, history=[]),
    log("EMP0005", "2026-07-06", "PRESENT", "2026-07-06T16:25:00Z", "2026-07-07T01:10:00Z", 8.75, 0, 40, history=[]),  # overnight
    log("EMP0001", "2026-07-07", "PRESENT", "2026-07-07T03:58:00Z", "2026-07-07T13:00:00Z", 9.03, 0, 0, history=[
        {"at": d("2026-07-08T05:00:00Z"), "by": "hr.admin", "reason": "biometric glitch",
         "changes": {"punch_in": {"from": d("2026-07-07T04:35:00Z"), "to": d("2026-07-07T03:58:00Z")},
                     "late_minutes": {"from": 35, "to": 0}, "work_hours": {"from": 8.42, "to": 9.03}}}]),
    log("EMP0002", "2026-07-14", "PRESENT", "2026-07-14T04:02:00Z", "2026-07-14T13:01:00Z", 8.98, 0, 0, half=None),  # legacy
    log("EMP0001", "2026-07-04", "PRESENT", "2026-07-04T04:20:00Z", "2026-07-04T12:20:00Z", 8.0, 20, 0, history=[]),   # Saturday
    log("EMP0004", "2026-07-09", "PRESENT", "2026-07-09T04:35:00Z", "2026-07-09T13:35:00Z", 9.0, 35, 0, history=[]),   # tie with EMP0003
    log("EMP0002", "2026-07-13", "PRESENT", "2026-07-13T04:20:00Z", "2026-07-13T12:50:00Z", 8.5, 20, 0, history=[]),   # tie with EMP0001
    log("EMP9999", "2026-07-06", "PRESENT", "2026-07-06T04:35:00Z", "2026-07-06T13:40:00Z", 9.0, 100, 0, history=[]),  # ghost employee
    log("EMP0005", "2026-07-11", "ON_DUTY", "2026-07-11T16:30:00Z", "2026-07-12T00:31:12Z", 8.02, 0, 0, history=[]),   # Saturday, on duty
]


def seed():
    db = MongoClient(URI)[DBNAME]
    db.employees.delete_many({})  # keep indexes
    db.attendance_logs.delete_many({})
    db.employees.insert_many(json_util.loads(json.dumps(EMPLOYEES)))
    db.attendance_logs.insert_many(json_util.loads(json.dumps(LOGS)))


def start_server():
    env = {**os.environ, "MONGO_URI": URI, "MONGO_DB": DBNAME}
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", "8765"], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            if httpx.get(BASE + "/health").status_code == 200:
                return p
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    p.kill()
    raise SystemExit("server did not become healthy")


def parallel(fn, n=10):
    with ThreadPoolExecutor(n) as ex:
        return sorted(r.status_code for r in ex.map(lambda _: fn(), range(n)))


def run(c: httpx.Client):
    # ---- employees ----------------------------------------------------------------------------------------
    body = dict(emp_code="EMP0100", name="New", email="new@example.com", department="Sales", joined_on="2026-07-01")
    r = c.post("/employees", json=body)
    assert r.status_code == 201 and isinstance(r.json()["created_at"], int) and "_id" not in r.json(), r.text
    assert r.json()["shift_start"] == "09:30"
    assert c.post("/employees", json=body).status_code == 409
    assert c.post("/employees", json={**body, "emp_code": "X1"}).status_code == 422
    assert c.post("/employees", json={**body, "emp_code": "EMP0101", "shift_end": "09:30"}).status_code == 422
    assert c.post("/employees", json={**body, "emp_code": "EMP0101", "email": "nope"}).status_code == 422
    assert c.post("/employees", json={**body, "emp_code": "EMP0101", "joined_on": "2026-02-30"}).status_code == 422
    dup = {**body, "emp_code": "EMP0102"}
    assert parallel(lambda: c.post("/employees", json=dup)) == [201] + [409] * 9, "duplicate-create race"

    r = c.get("/employees", params={"department": "Sales", "page_size": 2}).json()
    assert r["total"] == 4 and [e["emp_code"] for e in r["items"]] == ["EMP0003", "EMP0004"], r
    r = c.get("/employees", params={"department": "Sales", "page_size": 2, "page": 2}).json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP0100", "EMP0102"]
    assert c.get("/employees", params={"page": 0}).status_code == 422
    assert c.get("/employees", params={"page_size": 101}).status_code == 422

    # ---- punch-in (R1, R2) --------------------------------------------------------------------------------
    r = c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-15T09:40:01+05:30") + 900})
    assert r.status_code == 201, r.text
    rec = r.json()
    assert rec["date"] == "2026-07-15" and rec["late_minutes"] == 10, rec
    assert rec["punch_in"] == ms("2026-07-15T09:40:01+05:30"), "must truncate to whole seconds"
    assert rec["work_hours"] is None and rec["history"] == [] and "id" not in rec and "_id" not in rec
    assert c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-15T10:00:00+05:30")}).status_code == 409
    assert c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783312500}).status_code == 422  # seconds
    assert c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783312500000.0}).status_code == 422
    assert c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": "1783312500000"}).status_code == 422
    assert c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "ABSENT"}).status_code == 422
    assert c.post("/attendance/punch-in", json={"emp_code": "EMP4242"}).status_code == 404
    # grace boundary: 09:40:00 -> 0 ; 10:15:59 -> 45
    r = c.post("/attendance/punch-in", json={"emp_code": "EMP0003", "punched_at": ms("2026-07-15T09:40:00+05:30")}).json()
    assert r["late_minutes"] == 0, r
    r = c.post("/attendance/punch-in", json={"emp_code": "EMP0004", "punched_at": ms("2026-07-15T10:15:59+05:30"), "status": "WFH"}).json()
    assert r["late_minutes"] == 45 and r["status"] == "WFH", r
    # overnight: 01:00 IST on the 17th belongs to the shift that started on the 16th; late since 22:00
    r = c.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms("2026-07-17T01:00:00+05:30")}).json()
    assert r["date"] == "2026-07-16" and r["late_minutes"] == 180, r
    assert parallel(lambda: c.post("/attendance/punch-in", json={"emp_code": "EMP0006", "punched_at": ms("2026-07-20T09:00:00+05:30")})) \
        == [201] + [409] * 9, "punch-in race"

    # ---- punch-out (R3-R5) --------------------------------------------------------------------------------
    assert c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-15T09:40:01+05:30")}).status_code == 422
    assert c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-16T09:40:02+05:30")}).status_code == 422
    assert c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-15T09:00:00+05:30")}).status_code == 409  # hits 07-07, already closed
    assert c.post("/attendance/punch-out", json={"emp_code": "EMP0100", "punched_at": ms("2026-07-15T18:00:00+05:30")}).status_code == 404
    assert c.post("/attendance/punch-out", json={"emp_code": "EMP4242"}).status_code == 404
    r = c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-15T18:35:00+05:30")})
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["work_hours"] == 8.92 and rec["overtime_minutes"] == 0 and rec["half_day"] is False, rec  # 8h54m59s, 5 min OT < 30
    assert c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-15T18:36:00+05:30")}).status_code == 409
    # half day + overtime threshold + half-up rounding: 4h29m30s = 4.4917 -> 4.49 < 4.50
    r = c.post("/attendance/punch-out", json={"emp_code": "EMP0003", "punched_at": ms("2026-07-15T14:09:30+05:30")}).json()
    assert r["work_hours"] == 4.49 and r["half_day"] is True, r
    # overnight punch-out across midnight: shift end is 06:00 next day -> 40 min OT
    r = c.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ms("2026-07-17T06:40:00+05:30")}).json()
    assert r["date"] == "2026-07-16" and r["work_hours"] == 5.67 and r["overtime_minutes"] == 40, r
    assert parallel(lambda: c.post("/attendance/punch-out", json={"emp_code": "EMP0006", "punched_at": ms("2026-07-20T18:00:00+05:30")})) \
        == [200] + [409] * 9, "punch-out race"

    # ---- regularize ---------------------------------------------------------------------------------------
    fix = {"reason": "forgot to punch", "regularized_by": "hr.admin"}
    r = c.patch("/attendance/EMP0004/2026-07-08", json={**fix, "status": "PRESENT", "punch_in": ms("2026-07-08T09:30:00+05:30"),
                                                        "punch_out": ms("2026-07-08T18:30:00+05:30"), "late_minutes": 999})
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["status"] == "PRESENT" and rec["work_hours"] == 9.0 and rec["late_minutes"] == 0, rec
    assert len(rec["history"]) == 1 and set(rec["history"][0]["changes"]) == {"status", "punch_in", "punch_out", "work_hours"}, rec
    assert rec["history"][0]["changes"]["punch_in"] == {"from": None, "to": ms("2026-07-08T09:30:00+05:30")}
    assert isinstance(rec["history"][0]["at"], int)
    assert c.patch("/attendance/EMP0004/2026-07-08", json={**fix, "status": "PRESENT"}).status_code == 422  # nothing changes
    assert c.patch("/attendance/EMP0004/2026-07-08", json={**fix, "status": "LEAVE", "punch_in": ms("2026-07-08T09:30:00+05:30")}).status_code == 422
    assert c.patch("/attendance/EMP0004/2026-07-08", json={**fix, "punch_in": ms("2026-07-09T09:30:00+05:30")}).status_code == 422  # wrong day
    assert c.patch("/attendance/EMP0004/2026-07-08", json={**fix, "punch_out": ms("2026-07-08T09:00:00+05:30")}).status_code == 422  # before in
    assert c.patch("/attendance/EMP0004/2026-07-08", json={"reason": "x", "regularized_by": "hr"}).status_code == 422
    assert c.patch("/attendance/EMP0004/2026-07-08", json={**fix, "punch_in": None}).status_code == 422
    assert c.patch("/attendance/EMP0004/2026-07-30", json={**fix, "status": "LEAVE"}).status_code == 404
    assert c.patch("/attendance/EMP4242/2026-07-08", json={**fix, "status": "LEAVE"}).status_code == 404
    r = c.patch("/attendance/EMP0004/2026-07-08", json={**fix, "status": "LEAVE"}).json()
    assert r["punch_in"] is None and r["work_hours"] is None and len(r["history"]) == 2
    assert r["history"][1]["changes"]["punch_out"] == {"from": ms("2026-07-08T18:30:00+05:30"), "to": None}
    assert c.patch("/attendance/EMP0003/2026-07-08", json={**fix, "status": "PRESENT"}).status_code == 422  # presence needs punch_in
    r = c.patch("/attendance/EMP0002/2026-07-14", json={**fix, "status": "WFH"}).json()  # legacy doc
    assert r["half_day"] is False and len(r["history"]) == 1 and r["history"][0]["changes"] == {"status": {"from": "PRESENT", "to": "WFH"}}
    # concurrent corrections: every 200 leaves exactly one history entry, losers get 409
    def patch(i):
        return c.patch("/attendance/EMP0001/2026-07-06", json={**fix, "punch_in": ms("2026-07-06T09:00:00+05:30") + i * 60000})
    with ThreadPoolExecutor(10) as ex:
        codes = sorted(r.status_code for r in ex.map(patch, range(10)))
    assert set(codes) <= {200, 409} and codes.count(200) >= 1, codes
    hist = c.get("/attendance", params={"emp_code": "EMP0001", "date_from": "2026-07-06", "date_to": "2026-07-06"}).json()["items"][0]["history"]
    assert len(hist) == codes.count(200), (len(hist), codes)

    # ---- list attendance (R10) ----------------------------------------------------------------------------
    r = c.get("/attendance", params={"date_from": "2026-07-06", "date_to": "2026-07-07", "page_size": 3}).json()
    assert r["total"] == 7 and [(x["date"], x["emp_code"]) for x in r["items"]] == \
        [("2026-07-07", "EMP0001"), ("2026-07-07", "EMP0004"), ("2026-07-07", "EMP0006")], r
    legacy = c.get("/attendance", params={"emp_code": "EMP0002", "status": "WFH"}).json()
    assert legacy["total"] == 1 and legacy["items"][0]["half_day"] is False and isinstance(legacy["items"][0]["punch_in"], int)
    assert all("id" not in x and "_id" not in x for x in r["items"])
    assert c.get("/attendance", params={"date_from": "2026-07-08", "date_to": "2026-07-07"}).status_code == 422
    assert c.get("/attendance", params={"date_from": "2026-13-01"}).status_code == 422
    assert c.get("/attendance", params={"date_from": "2026-02-30"}).status_code == 422
    assert c.get("/attendance", params={"status": "SICK"}).status_code == 422
    assert c.get("/attendance", params={"page_size": 0}).status_code == 422

    # ---- analytics: fresh seed so the numbers are hand-checkable ------------------------------------------
    seed()
    m = c.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"}).json()
    assert m == dict(emp_code="EMP0001", month="2026-07", working_days=23, present_days=2, leave_days=0, late_count=1,
                     total_late_minutes=20, total_overtime_minutes=0, attendance_pct=8.7), m  # Sat record: late yes, present no
    m = c.get("/analytics/employees/EMP0002/monthly", params={"month": "2026-07"}).json()
    assert m["working_days"] == 15 and m["present_days"] == 2 and m["attendance_pct"] == 13.33, m  # mid-month joiner
    m = c.get("/analytics/employees/EMP0007/monthly", params={"month": "2026-07"}).json()
    assert m["working_days"] == 0 and m["attendance_pct"] is None and m["present_days"] == 0, m
    m = c.get("/analytics/employees/EMP0004/monthly", params={"month": "2026-07"}).json()
    assert m["present_days"] == 1.5 and m["late_count"] == 1, m  # half day
    m = c.get("/analytics/employees/EMP0003/monthly", params={"month": "2026-07"}).json()
    assert m["leave_days"] == 1 and m["total_overtime_minutes"] == 40, m
    assert c.get("/analytics/employees/EMP4242/monthly", params={"month": "2026-07"}).status_code == 404
    assert c.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-7"}).status_code == 422
    assert c.get("/analytics/employees/EMP0001/monthly").status_code == 422

    s = c.get("/analytics/departments/summary", params={"month": "2026-07"}).json()
    assert s["month"] == "2026-07" and [x["department"] for x in s["items"]] == ["Engineering", "Sales", "Support"], s
    eng, sales, sup = s["items"]
    assert eng == dict(department="Engineering", headcount=2, present_days=4, avg_work_hours=8.73, late_count=2,
                       total_late_minutes=40, leave_count=0, on_duty_count=0), eng  # EMP0007 joins in Aug: excluded
    assert sales == dict(department="Sales", headcount=2, present_days=2.5, avg_work_hours=7.19, late_count=2,
                         total_late_minutes=70, leave_count=1, on_duty_count=0), sales
    assert sup == dict(department="Support", headcount=2, present_days=2, avg_work_hours=8.39, late_count=1,
                       total_late_minutes=10, leave_count=0, on_duty_count=1), sup  # 8.385 half-up; open record excluded
    s = c.get("/analytics/departments/summary", params={"month": "2026-08"}).json()
    assert [(x["department"], x["headcount"], x["avg_work_hours"], x["present_days"]) for x in s["items"]] == \
        [("Engineering", 3, None, 0), ("Sales", 2, None, 0), ("Support", 2, None, 0)], s  # no logs at all
    s = c.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Sales"}).json()
    assert len(s["items"]) == 1 and s["items"][0]["department"] == "Sales"
    assert c.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Nope"}).json()["items"] == []

    lb = c.get("/analytics/leaderboard/late", params={"month": "2026-07"}).json()
    assert [(x["rank"], x["emp_code"], x["total_late_minutes"]) for x in lb["items"]] == \
        [(1, "EMP0003", 35), (1, "EMP0004", 35), (3, "EMP0001", 20), (3, "EMP0002", 20), (5, "EMP0006", 10)], lb  # ghost ignored
    assert lb["items"][0]["name"] == "Neha Iyer" and lb["items"][0]["department"] == "Sales" and lb["items"][0]["late_count"] == 1
    lb = c.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 1}).json()
    assert [x["emp_code"] for x in lb["items"]] == ["EMP0003", "EMP0004"], "tie at the cutoff keeps both"
    lb = c.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 3}).json()
    assert len(lb["items"]) == 4
    lb = c.get("/analytics/leaderboard/late", params={"month": "2026-07", "department": "Engineering"}).json()
    assert [(x["rank"], x["emp_code"]) for x in lb["items"]] == [(1, "EMP0001"), (1, "EMP0002")]
    assert c.get("/analytics/leaderboard/late", params={"month": "2026-06"}).json()["items"] == []
    assert c.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 51}).status_code == 422

    t = c.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-03", "to": "2026-07-14"}).json()
    assert t["department"] == "Engineering" and len(t["items"]) == 12
    rows = [(x["date"][5:], x["is_working_day"], x["headcount"], x["present_count"], x["late_count"], x["attendance_rate"], x["moving_avg_7d"]) for x in t["items"]]
    expected = [
        ("07-03", True, 1, 0, 0, 0.0, 0.0),
        ("07-04", False, 1, 1, 1, None, 0.0),       # Saturday record counts, rate null on weekends
        ("07-05", False, 1, 0, 0, None, 0.0),
        ("07-06", True, 1, 1, 0, 1.0, 0.5),
        ("07-07", True, 1, 1, 0, 1.0, 0.6667),
        ("07-08", True, 1, 0, 0, 0.0, 0.5),
        ("07-09", True, 1, 0, 0, 0.0, 0.4),
        ("07-10", True, 1, 0, 0, 0.0, 0.4),        # 07-03 left the 7-row window
        ("07-11", False, 1, 0, 0, None, 0.4),
        ("07-12", False, 1, 0, 0, None, 0.4),
        ("07-13", True, 2, 1, 1, 0.5, 0.3),        # EMP0002 joined: headcount 2
        ("07-14", True, 2, 1, 0, 0.5, 0.2),
    ]
    assert rows == expected, "\n".join(f"{a}\n{b}" for a, b in zip(rows, expected) if a != b)
    t = c.get("/analytics/departments/Support/trend", params={"from": "2026-08-01", "to": "2026-08-03"}).json()
    assert [(x["date"], x["headcount"], x["attendance_rate"], x["moving_avg_7d"]) for x in t["items"]] == \
        [("2026-08-01", 2, None, None), ("2026-08-02", 2, None, None), ("2026-08-03", 2, 0.0, 0.0)], t  # no logs at all
    assert c.get("/analytics/departments/Nope/trend", params={"from": "2026-07-01", "to": "2026-07-02"}).status_code == 404
    assert c.get("/analytics/departments/Sales/trend", params={"from": "2026-07-02", "to": "2026-07-01"}).status_code == 422
    assert c.get("/analytics/departments/Sales/trend", params={"from": "2026-07-01", "to": "2026-09-30"}).status_code == 200  # 92 days
    assert c.get("/analytics/departments/Sales/trend", params={"from": "2026-07-01", "to": "2026-10-01"}).status_code == 422
    assert c.get("/analytics/departments/Sales/trend", params={"from": "2026-07-01"}).status_code == 422

    # ---- explain ------------------------------------------------------------------------------------------
    for ep, params in [("attendance_list", {}), ("attendance_list", {"status": "PRESENT", "date_from": "2026-07-01", "date_to": "2026-07-31"}),
                       ("attendance_list", {"emp_code": "EMP0001"}),
                       ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
                       ("department_summary", {"month": "2026-07"}), ("department_summary", {"month": "2026-07", "department": "Sales"}),
                       ("late_leaderboard", {"month": "2026-07", "limit": 3}),
                       ("department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-31"})]:
        r = c.get(f"/admin/explain/{ep}", params=params)
        assert r.status_code == 200, (ep, r.text)
        body = r.json()
        txt = json.dumps(body["explain"])
        assert body["endpoint"] == ep and "IXSCAN" in txt and "COLLSCAN" not in txt, (ep, params, txt[:3000])
    assert c.get("/admin/explain/employee_monthly", params={"month": "2026-07"}).status_code == 422
    assert c.get("/admin/explain/department_trend", params={"department": "Sales"}).status_code == 422
    assert c.get("/admin/explain/nope", params={"month": "2026-07"}).status_code == 422


if __name__ == "__main__":
    seed()
    server = start_server()
    try:
        with httpx.Client(base_url=BASE, timeout=30) as c:
            run(c)
        print("ALL CHECKS PASSED")
    finally:
        server.kill()
