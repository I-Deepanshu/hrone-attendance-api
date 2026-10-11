# Employee Attendance & Analytics API

FastAPI + MongoDB (6.0+). All application code is in `app/main.py`.

## Run
```bash
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"   # real env vars win over .env
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```
Indexes are created at startup (idempotent). `GET /health` returns 200 once MongoDB answers a ping.

Local MongoDB: `docker run -d -p 27017:27017 mongo:7`, then `python sample_seed.py` to load the nine sample documents.

## Test
`python test_api.py` starts the app on a scratch database (`MONGO_TEST_DB`, default `attendance_test`), seeds awkward cases
(ties, weekend records, a mid-month joiner, an employee with no logs, an overnight shift, a legacy document, a ghost
employee) and asserts status codes, R1-R10 numbers, the three races and the explain plans.

## Things to know
* Instants are truncated to whole seconds at the API boundary (R1) and stored as UTC BSON dates; the client is `tz_aware`.
* Reported numbers are rounded half-up with `Decimal` in Python and with a `$toDecimal` floor expression in pipelines, because `$round` is half-even.
* Regularisation uses an optimistic lock on the history length: a concurrent correction that lands first makes the second one a 409.
* Trend range: at most 92 calendar days inclusive (`from` = 1 July, `to` = 30 September is accepted).
* Analytics are single aggregation pipelines; the only Python arithmetic is the working-day calendar count (R7) for the employee monthly summary.
* `GET /admin/explain/{endpoint}` runs the same query builder as the endpoint with `executionStats`; on 100k records every plan is an IXSCAN with no COLLSCAN.
