# REVIEW.md

Line numbers refer to the starter `app/main.py` as received. Every fix below is exercised by `test_api.py`.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` L35 | Shift start is built with `punch_in.replace(hour, minute)`: same calendar day and same (naive, non-IST) time zone as the punch. R1 says shift times are IST, and an overnight punch after midnight belongs to a shift that started the day before. | Punch 09:40:01 IST = 04:10:01 UTC on a UTC server: start becomes 09:30 *UTC*, diff is negative, late = 0. Overnight 22:00 shift, punch 01:00 IST: late = 0 instead of 180. | `shift_bounds()` builds start/end from the attendance date in IST; overnight shifts put the end on the next day. |
| 2 | `compute_late_minutes` L36-37 | Grace is compared in floored minutes: `int(10.016) > 10` is false. R2: late if strictly more than 10:00 *seconds-precise*. | Shift 09:30, punch 09:40:01 → 0; contract says 10. | Compare seconds (`> 600`), then floor minutes since shift start. |
| 3 | `compute_work_hours` L41 | Python `round()` is banker's rounding on a float; R4 demands half-up. | 8h07m30s = 8.125 h → `round` gives 8.12, contract 8.13. | `Decimal(seconds) / 3600` quantized with `ROUND_HALF_UP`. |
| 4 | `compute_overtime` L47 | No 30-minute minimum (R3). | Punch-out 18:35 on an 18:30 shift → 5, contract 0. | `ot if ot >= 30 else 0`. |
| 5 | `compute_overtime` L46 | Shift end is placed on `date_str` itself. For an overnight shift the end is on the next day. Also an aware `end` minus a naive `punch_out` raises `TypeError` → 500. | Sample doc 7 (22:00-06:00, out 06:40 next day) → 1480 min instead of 40. | Overnight end = date + 1 day; all datetimes are UTC-aware (`tz_aware=True`). |
| 6 | helpers in general | No truncation of instants to whole seconds (R1); ms kept in stored/returned values. | `punched_at = ...500900` is returned as `...500900`; contract `...500000`. | `ms_to_dt()` floors to seconds at the boundary. |
| 7 | `health` L72-74 | Never talks to MongoDB; always 200. | Stop Mongo: `/health` still says ok; grader's readiness probe lies. | `db.command("ping")` → 503 on `PyMongoError`; `serverSelectionTimeoutMS=5000` so it answers inside the 20 s window. |
| 8 | `create_employee` L79-80 | Check-then-insert; no unique index. | 10 parallel POSTs with the same `emp_code` → several 201s and duplicate documents. | Unique index on `emp_code`; `insert_one` and map `DuplicateKeyError` → 409. |
| 9 | `create_employee` L82, L85 | `datetime.now()` is naive local time (stored with the server's offset baked in); `created_at` is returned as an ISO string, contract says epoch ms. | Response `created_at: "2026-10-10T22:30:00.123"`. | `now_utc()` (aware, ms precision) and `dt_to_ms()` on output. |
| 10 | `EmployeeIn` L53-60 | No validation at all: `emp_code` pattern, email, lengths, `HH:MM`, `joined_on`, `shift_start != shift_end`. | `emp_code="X1"`, `email="nope"`, `shift_end == shift_start`, `joined_on="2026-02-30"` → all 201. | Pydantic `Field` constraints + `model_validator`. |
| 11 | `list_employees` L93 | `skip = page * page_size` skips the first page. | `page=1&page_size=2` returns employees 3-4. | `(page - 1) * page_size`. |
| 12 | `list_employees` L94 | `total` counts the whole collection, ignoring `department` (R10). | `?department=Sales` → `total` = all employees. | `count_documents(q)`. |
| 13 | `list_employees` L95 | No sort; contract says `emp_code` ascending. | Insertion order leaks out. | `.sort("emp_code")`. |
| 14 | `list_employees` / `list_attendance` L89, L130 | `page` / `page_size` unbounded. | `page_size=100000` is honoured; `page=-1` → negative skip → PyMongo `ValueError` → 500. | `Query(ge=1)`, `Query(ge=1, le=100)` → 422. |
| 15 | `punch_in` L101 | Unknown employee is not handled; `emp["shift_start"]` on `None`. | `emp_code=EMP4242` → 500, contract 404. | `get_employee()` raises 404. |
| 16 | `punch_in` L102-103 | `fromtimestamp(ms/1000)` gives naive server-local time; `date` is the server's calendar date, not IST; no overnight rule. | Day-shift employee punching at 01:00 IST on the 17th (19:30 UTC on the 16th): a UTC server writes `date = 2026-07-16`, contract `2026-07-17`. Conversely the overnight sample (punch 21:55 IST) is right only by luck, and a 01:00 IST overnight punch never maps to the previous day. | `attendance_date()` implements R1 in IST. |
| 17 | `punch_in` L104-105, L118 | Check-then-insert race: two simultaneous punch-ins both pass `find_one` and both insert. | 10 parallel punch-ins → 10 × 201 and 10 documents for one day. | Unique `(emp_code, date)` index; `DuplicateKeyError` → 409. No pre-check. |
| 18 | `PunchInIn` L63-66 | `status` is free text, `punched_at` any int. | `status="ABSENT"` accepted; `punched_at=1783312500` (seconds) → a 1970 record; floats/strings accepted. | `Literal[PRESENT, WFH, ON_DUTY]`; `EpochMs` = strict int in `[1e11, 4102444800000]`. |
| 19 | `punch_in` L119-121 | Response carries `id` (ObjectId) and raw datetimes. | `"id": "66..."`, `"punch_in": "2026-07-15T04:10:01"`. | `record_out()` emits exactly the contract fields, instants as ms. |
| 20 | `list_attendance` L144-147 | Loads every matching document into Python, sorts and slices in memory; sort key ignores the `emp_code` tiebreak. | 100k records → ~100k docs transferred per page request; order within a day is random. | `find(q).sort(date desc, emp_code asc).skip().limit()` + `count_documents`; index `(date:-1, emp_code:1)` matches the sort exactly. |
| 21 | `list_attendance` L126-129 | Dates and status are not validated. | `date_from > date_to` → 200 and empty; `date_from=2026-13-01`, `status=SICK` → 200. | Regex + `date.fromisoformat` → 422; `status: Literal[...]`. |
| 22 | `list_attendance` L148-149 | Returns `id`; legacy docs come back without `half_day` / `history` (contract: required, missing means false / `[]`). | Sample doc 9 lacks both fields in the response. | `record_out()` normalises defaults. |
| 23 | module level L22 | No indexes created at startup; `MongoClient` not `tz_aware` (naive datetimes from the DB mixed with aware ones → `TypeError` 500); no server-selection timeout (`/health` would hang ~30 s with the DB down). | Every race above; any aware/naive subtraction crashes; readiness never reported within 20 s when Mongo is slow. | `lifespan` → `ensure_indexes()` (idempotent, also retried from `/health` if startup ran before Mongo was up); `tz_aware=True`; `serverSelectionTimeoutMS=5000`. |

## Looked at and left alone

* `load_dotenv()` without `override=True`: real environment variables already win over `.env`, which is what the contract asks for.
* `IST = timezone(timedelta(hours=5, minutes=30))` is correct.
* One module-level `MongoClient`: it connects lazily, is thread-safe, and FastAPI runs sync endpoints in a thread pool, so concurrent requests are fine.
* `doc.pop("_id")` after `insert_one`: PyMongo mutates the inserted dict, so the pop is needed; I kept the idea but serialise through one function instead.
* The `localhost:27017` / `attendance_db` fallbacks: kept as a local convenience only. The env vars always take precedence, nothing else is hard-coded.
* `int(x / 60)` truncates toward zero rather than flooring. For the positive values that matter it is the same; I switched to `//` anyway so negative differences cannot surprise anyone.
