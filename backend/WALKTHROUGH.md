# ARS backend — walkthrough

This explains how the backend works, file by file, so you can study it after the deadline.
Everything here was tested end to end: `scripts/demo_run.py` passes 27/27 checks, and the unit tests pass 43/43.

---

## 1. Run it

Three PowerShell windows, all in `backend`, all with `.venv` active.

```powershell
# window 1 — services + server
docker compose up -d
python -m pip install -r requirements.txt          # first time, or after requirements change
docker compose exec db psql -U ars -d ars -f /sql/schema.sql   # only for a clean database
docker compose exec db psql -U ars -d ars -f /sql/seed.sql
python -m uvicorn app.main:app --reload --loop asyncio:SelectorEventLoop

# window 2 — watch every MQTT message
docker compose exec mosquitto mosquitto_sub -t "ars/#" -v

# window 3 — tests and the end-to-end demo
python -m pytest -q
python scripts/demo_run.py
```

`/health` should return `{"db":"ok","mqtt":"connected"}`. The demo should end with `27/27 checks passed`.

> Windows: always start uvicorn with `--loop asyncio:SelectorEventLoop`. The default Windows loop (Proactor) breaks both async psycopg and aiomqtt.

The broker is published on host port `1884` because the Windows Mosquitto service may already use `1883`; inside Docker, the API still connects to Mosquitto on `1883`.

---

## 2. The big picture

```
 clicker / bridge / demo script                      teacher (browser, /docs)
        |  MQTT: ars/room1/dev/07/answer                       |  HTTP: POST /sessions/1/next
        v                                                      v
   [Mosquitto] --------------------------> [FastAPI app] <------+
        ^        app/mqtt.py (worker)       |  app/routers/*.py
        |                                   |
        +--- ack + retained state ----------+
                                            |
                                     [PostgreSQL]
             raw_events  ->  session_devices / answers  ->  v_ranking / v_question_stats
             (raw layer)     (cleaned layer)                (results layer)
```

Two ways in:
- **HTTP** (teacher): create quizzes and drive the session: start → next → close → … → end.
- **MQTT** (devices): register and answer. The backend replies with an **ack** on the device's own topic, and publishes the session **state** as a retained message.

---

## 3. Files, in the order a request touches them

| File | Job | Key idea to learn |
|---|---|---|
| `app/config.py` | Reads `.env` into typed settings | pydantic-settings; one place for config |
| `app/db.py` | Creates the connection pool | pool = reuse connections; `dict_row`; `autocommit` + explicit transactions |
| `app/queries.py` | Loads every `-- name:` block from `sql/queries.sql` | keep SQL in SQL files; named placeholders `%(name)s` |
| `app/main.py` | Lifespan: open pool, start MQTT worker; register routers | startup/shutdown with `yield`; background task |
| `app/models.py` | Shapes of API requests/responses | validation → automatic 422 errors; `model_validator` compares fields |
| `app/state_machine.py` | Allowed phase transitions | pure function + table of transitions; easy to test |
| `app/rules.py` | Decides the ack code for each message | pure functions; order of checks matters |
| `app/mqtt.py` | Worker: raw → rules → cleaned → status → ack | the data pipeline; reconnect loop |
| `app/routers/quizzes.py` | Create/read quizzes | one transaction for quiz + questions |
| `app/routers/sessions.py` | Teacher drives the session; auto-close timer | state machine + guarded UPDATE + publish retained state |
| `app/routers/results.py` | Ranking and statistics | read from views, no maths in Python |
| `sql/schema.sql` | Tables, constraints, views | the three data layers |
| `sql/queries.sql` | Every statement the app runs | guards, upserts, RETURNING |
| `tests/` | Unit tests for the pure parts | parametrised tests |
| `scripts/demo_run.py` | Whole quiz through the real API + MQTT | end-to-end test |

---

## 4. What happens when a clicker answers

Message: topic `ars/room1/dev/07/answer`, payload `{"dev":7,"q":1,"choice":2,"seq":5}`.

1. **Worker receives it** (`run_worker` in `app/mqtt.py`) and calls `handle_message`.
2. **Parse the topic** (`parse_topic`): room `room1`, device `7`, kind `answer`. A topic that does not match is stored with status `BAD` and gets no ack (we cannot tell who sent it).
3. **Parse the payload** (`parse_payload`): JSON → dict, or `None` if not JSON.
4. **Find the active session** for `room1` (`find_active_session`).
5. **Raw layer first**: `insert_raw_event` stores topic, text, JSON (or NULL), session id. The row's `received_at` is the database clock — the only clock used for deadlines.
6. **Load the context in one query** (`answer_context`): phase, current question, deadline, the question's id and number of choices, whether device 7 is registered, and its `last_seq`.
7. **Rules** (`check_answer` in `app/rules.py`), first failing check wins:
   `BAD` (shape, dev mismatch, choice out of range) → `UNREGISTERED` → `CLOSED` (wrong phase, wrong question, after deadline) → `DUP` (seq not newer) → `OK`.
8. **Cleaned layer** (only if OK), inside one transaction:
   `save_answer` (upsert that never goes backwards) + `bump_last_seq`.
9. **Status**: `set_event_status` writes the final code on the raw row.
10. **Ack**: publish `{"seq":5,"code":"OK"}` to `ars/room1/dev/07/ack`.

If anything throws, the worker logs it and continues with the next message — one bad message never stops ingestion.

---

## 5. What happens when the teacher clicks "next"

`POST /sessions/1/next` → `_transition(app, 1, Event.NEXT)` in `app/routers/sessions.py`:

1. Read the session (404 if missing).
2. **State machine**: `next_phase(phase, "next")` → 409 with a clear message if not allowed (for example `'next' is not allowed in phase IDLE; allowed: ['start']`).
3. **Guarded UPDATE** `open_next_question`: sets `current_q + 1`, `q_opened_at = now()`, `q_deadline = now() + time_limit`, but only `WHERE phase IN ('REGISTER','QUESTION_CLOSED')` and only if that question exists. 0 rows → 409 "no more questions".
4. **Timer**: an `asyncio` task sleeps until the deadline, then runs `close_question` **for that question number only** — so a late timer can never close the next question.
5. **Publish the retained state** `{"phase":"QUESTION_OPEN","q":1,"n_choices":4,"deadline_ms":20000}` to `ars/room1/state`. Every clicker (and any that reconnect later) sees it.

---

## 6. Where the data engineering is

| Concept | Where |
|---|---|
| Raw / cleaned / results layers (bronze / silver / gold) | `raw_events` → `answers` → views |
| At-least-once delivery + idempotent writes | QoS 1 + `seq` + guarded upsert |
| Late / out-of-order events | `received_at` vs `q_deadline`; `WHERE answers.seq < EXCLUDED.seq` |
| Data quality metrics | `v_session_event_stats` (count per status) |
| Schema constraints as data contracts | CHECK, UNIQUE, composite FK, partial unique index |
| Single source of time | database `now()` only |

These are exactly the topics a data-engineering interviewer asks about. `DECISIONS.md` has the reasoning for each.

---

## 7. Try these to learn (change, predict, run, explain)

1. In `app/rules.py`, swap the order of the `UNREGISTERED` and `CLOSED` checks. Which demo check fails, and why does the order matter?
2. Remove the `WHERE answers.seq < EXCLUDED.seq` line from `save_answer`. Run the demo. What happens to device 2's answer on question 1?
3. Change the subscriptions to `ars/+/dev/+/+`. Run the demo and look at `raw_events`. What appears that should not?
4. Set question 1's time limit to 10 s in `demo_run.py`. What happens to the "answers after the deadline" check, and why?
5. Stop Mosquitto (`docker compose stop mosquitto`), call `/health`, start it again. Read the server log: how does the worker recover?

---

## 8. What is NOT done yet (next steps)

- WebSocket push of live answer counts to the teacher screen.
- The teacher web page (for now, `/docs` is the teacher UI).
- Gemini comments on results, CSV export.
- The BLE bridge script (serial ↔ MQTT) — it publishes the same topics as `demo_run.py`.
- Latency test with 60 simulated devices (`demo_run.py` is a good starting point).
