# DECISIONS.md

1. **Indexes.** `employees`: `emp_code` unique (makes concurrent duplicate creates a 409, not a second document); `(department, emp_code)` for the filtered, sorted list and for the department match in summary/trend; `joined_on` for the summary's R9 match when no department is given. `attendance_logs`: `(emp_code, date)` unique (punch-in race, PATCH lookup, monthly summary); `(date desc, emp_code asc)` matches the list sort exactly and serves month ranges in the leaderboard; `(status, date desc, emp_code)` for the status filter; `(emp_code, punch_in desc)` for punch-out's "latest record" lookup. Considered and rejected: `(date, late_minutes)` for the leaderboard, because the month range already bounds the scan and the extra index would only be maintained for one query.

2. **Punch-in race.** Both requests load the employee, compute the IST date and late minutes, and call `insert_one`. There is no pre-check. The unique `(emp_code, date)` index lets exactly one insert land; the other raises `DuplicateKeyError`, which becomes 409. No transaction is needed.

3. **Ties.** `$setWindowFields` with `$rank` on total descending gives competition ranking (1, 2, 2, 4). `limit` is a `$match` on `rank <= limit` applied after ranking, so two people tied at rank 10 are both returned and the list has 11 rows, ordered by total descending then `emp_code`.

4. **Headcount.** The summary starts from `employees` filtered by `joined_on <= month end`, `$lookup`s each person's month of logs already grouped into one document, and `$unwind`s with `preserveNullAndEmptyArrays`. Someone with no logs survives as a row with no stats, counts `$sum: 1` toward headcount, and `$ifNull` turns their missing stats into 0.

5. **100x the data.** Maintain per-`(emp_code, month)` and per-`(department, day)` rollups written at punch-out and regularisation, so the analytics read a few hundred documents instead of scanning a month of logs; archive or shard raw logs by date.
