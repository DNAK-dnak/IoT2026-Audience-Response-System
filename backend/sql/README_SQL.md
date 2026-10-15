# ARS reference SQL

Tested on PostgreSQL 16. Put the `.sql` files in `backend/sql/` (they are then visible
inside the container as `/sql/...`).

| File | What it is | Load with |
|---|---|---|
| `schema.sql` | 11 tables, constraints, partial unique index, 4 result views | `docker compose exec db psql -U ars -d ars -f /sql/schema.sql` |
| `seed.sql` | 1 teacher, 1 course, 1 quiz with 3 questions (with option texts), devices 1-60, class DH22CNTT01 with 10 students | `docker compose exec db psql -U ars -d ars -f /sql/seed.sql` |
| `demo_session.sql` | A whole quiz run in SQL only, expected results in comments | `docker compose exec db psql -U ars -d ars -f /sql/demo_session.sql` |
| `queries.sql` | The statements the backend runs (36), with `%(name)s` placeholders for psycopg | Read it; copy each block into Python as needed |

Reload `schema.sql` then `seed.sql` any time you want a clean database (this deletes all data).

## How to compare with your own version

1. Load **your** schema + seed, then run `demo_session.sql` against it.
   If a step fails or a result differs from the comment, compare that table.
2. Things to check on purpose (each one is a design decision):
   - `answers` references `session_devices (session_id, device_no)` with a **composite foreign key**,
     so the database itself refuses answers from unregistered devices.
   - `sessions` has a CHECK that an open question always has `current_q`, `q_opened_at`, `q_deadline`.
   - `raw_events.device_no` has **no** foreign key on purpose: the raw layer must accept garbage.
   - `v_question_stats` gives two percentages: of students who answered, and of all registered students.
   - `mssv` is `TEXT` with a format CHECK (8 digits), never a number.
3. Differences are fine if you can explain them in `DECISIONS.md`.

## Known limitation (write it down)

If a device reboots in the middle of a question, its `seq` restarts at 1, so its next answer to
**that same question** looks older than the one already saved and is ignored. Re-registering resets
`last_seq`, and answers to later questions are unaffected.
