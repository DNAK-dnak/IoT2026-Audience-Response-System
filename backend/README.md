# Backend: running and reference

Server, database, broker, web pages, BLE bridge and test tools of the audience response system.
For the project overview see the [main README](../README.md).

## Services

| Service | Port on the laptop | Role |
|---|---|---|
| `api` (FastAPI) | **8000** | REST API, WebSocket, MQTT worker; serves the web pages |
| `mosquitto` | **1884** (MQTT), **9001** (MQTT over WebSocket, for phones) | Message broker |
| `db` (PostgreSQL 16) | 5432 | Database |

All three run in Docker (`docker-compose.yml`). The BLE bridge runs outside Docker, because Docker
on Windows cannot use Bluetooth. MQTT uses host port 1884 because a Windows Mosquitto service
often holds 1883; inside Docker the services still use 1883.

## Start and stop

```powershell
copy .env.example .env      # once
.\start.ps1                 # backend + teacher page + 50 simulated clickers
.\stop.ps1                  # close the helper windows and stop the containers (data is kept)
```

If Windows blocks the script: `Unblock-File .\start.ps1, .\stop.ps1`.

`start.ps1` checks Docker and Python, starts the containers, updates the database
(`sql/migrate_*.sql`, safe to run again, data kept), waits until `/health` is OK, opens the
helper windows and the teacher page, and prints a summary.

| Option | Effect |
|---|---|
| `-Pico` | Start the BLE bridge for the real clickers (`PicoRemote_<n>`) |
| `-PicoIds 7` | Numbers of the real clickers (`"7,12"` for several): the fake devices skip them |
| `-BrokenKey1 7` | Clicker 7 has a broken key 1: keys shift 2→1 … 9→8, answers are key + ENTER (see `BLE_BRIDGE.md`) |
| `-Phone` | BLE bridge in phone mode (`phone_ble.html`) |
| `-Fake 50` | Number of simulated clickers (`0` = none) |
| `-Test all` | Scenario of the simulated clickers: `normal` `burst` `lossy` `wrong_mssv` `reboot` `late` `dropout` `spam` `random` `all` |
| `-ClassId 1` | Class list the simulated clickers take their MSSVs from |
| `-Room room1` | Room (MQTT topic prefix) |
| `-Reset` | **Delete all data** and reload `schema.sql` + `seed.sql` |
| `-Watch` | Extra windows: backend log and all MQTT messages |
| `-OpenFirewall` | Once, as administrator: allow phones / a second laptop to reach ports 8000, 9001, 1884 |
| `-Server 192.168.1.23` | On a second laptop: run only the bridges, connected to the main laptop |
| `-NoBrowser` | Do not open the teacher page |

`.\stop.ps1 -KeepDocker` closes only the helper windows.

## Using it

- **Teacher page:** http://127.0.0.1:8000/. Always open this address: it redirects to the current
  version of the page, so the browser never runs an old cached copy. Demo logins:
  `GV001 / 123456`, `GV002 / abc123` (change them on the page).
- **Projector:** "Màn hình chiếu" button on the teacher page; drag the window to the projector, press F.
- **Phone clicker (Wi-Fi):** `http://<laptop IP>:8000/ui/phone.html?dev=51` (the summary prints the IP).
- **API documentation:** http://127.0.0.1:8000/docs
- **Health:** http://127.0.0.1:8000/health: `{"db":"ok","mqtt":"connected","analyze":true|false}`

**Demo data** (`sql/seed.sql`): class `DH22CNTT01` (id 1) with 60 students `22234001`–`22234060`,
device numbers 1–60, one demo quiz. The simulated clickers use the first N students of the class
list, so register a real clicker with an MSSV they do not use (e.g. `22234051`–`22234060` with 50
simulated clickers).

## Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `postgresql://ars:ars_dev_pw@localhost:5432/ars` | Used when running outside Docker; Docker Compose sets its own |
| `MQTT_HOST`, `MQTT_PORT` | `localhost`, `1883` | Broker (Docker Compose sets `mosquitto`) |
| `MQTT_USER`, `MQTT_PASS`, `MQTT_TLS` | empty, `false` | For a broker with login / TLS (e.g. HiveMQ Cloud) |
| `ROOM` | `room1` | Default room |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | empty, `gemini-3.6-flash` | AI summary with Gemini |
| `ANTHROPIC_API_KEY`, `ANALYZE_MODEL` | empty, `claude-sonnet-5` | AI summary with Claude |
| `AI_PROVIDER` | empty (= Gemini) | `gemini` or `anthropic` when both keys are set |

No key means the AI step is off and the page simply skips it. Keys live only in `.env`, which is
in `.gitignore`. Never commit it, and never put a key in the web page.

## Database

```powershell
# fresh database (DELETES all data), same as .\start.ps1 -Reset
docker compose exec -T db psql -U ars -d ars -f /sql/schema.sql
docker compose exec -T db psql -U ars -d ars -f /sql/seed.sql

# open a SQL prompt
docker compose exec db psql -U ars -d ars
```

Layers: setup tables (teachers, quizzes, questions, classes, devices) → `sessions`,
`session_devices` → `raw_events` (every MQTT message, insert-only) → `answers` (one locked answer
per device and question) → `answer_overrides` (teacher fixes) and `session_results` (final
result). Statistics are views: `v_effective_answers`, `v_question_stats`, `v_ranking`,
`v_score_distribution`, `v_session_event_stats`. All SQL used by the code is in `sql/queries.sql`.

## MQTT contract (devices)

| Topic | Direction | Payload |
|---|---|---|
| `ars/<room>/dev/<NN>/register` | device → server | `{"dev":7,"mssv":"22234007","seq":1}` |
| `ars/<room>/dev/<NN>/answer` | device → server | `{"dev":7,"q":2,"choice":3,"seq":4}` (choice 1–4 = A–D) |
| `ars/<room>/dev/<NN>/ack` | server → device | `{"seq":4,"code":"OK","q":2,"choice":3}` |
| `ars/<room>/state` (retained) | server → all | `{"phase":"QUESTION_OPEN","q":2,"n_choices":4,"deadline_ms":20000}` |

`<NN>` is the two-digit device number. Every message has a `seq`; a device that gets no ack
resends the same `seq`.

| Ack code | Meaning |
|---|---|
| `OK` | Registered / answer locked (`choice` = what the server locked) |
| `LOCKED` | This question already has a locked answer (`choice` = that answer) |
| `DUP` | Old message, or MSSV already used by another device |
| `CLOSED` | No question open / time is up / registration closed |
| `NOT_IN_CLASS` | MSSV not on the class list |
| `BAD` | Malformed message (MSSV not 8 digits, choice out of range, not JSON) |
| `UNREGISTERED` | Answer from a device that has not registered |
| `REMOVED` | The teacher removed this device; register again |

Session phases: `IDLE → REGISTER → QUESTION_OPEN ⇄ QUESTION_CLOSED → ENDED`.

## REST API (main endpoints)

| Endpoint | Purpose |
|---|---|
| `POST /auth/login`, `POST /auth/password` | Teacher login, change password |
| `POST /quizzes`, `GET /quizzes/{id}` | Save / read a quiz |
| `POST /classes`, `GET /classes`, `GET /classes/{id}/students` | Class lists |
| `POST /sessions`, `GET /sessions/active?room=` | Create a session / find the running one |
| `POST /sessions/{id}/start` · `next` · `close` · `end` | Drive the session |
| `GET /sessions/{id}/devices`, `DELETE …/devices[/{dev}]` | Registered devices; remove (registration only) |
| `GET /sessions/{id}/answers`, `PUT /sessions/{id}/answers` | Answer grid; teacher fix / manual entry |
| `GET /sessions/{id}/results` | Ranking, per-question stats, score distribution |
| `POST /sessions/{id}/results`, `GET …/saved-results` | Save / read the final result |
| `POST /sessions/{id}/analyze` | AI summary (503 without an API key) |
| `WS /sessions/{id}/live` | Live events for the teacher page (snapshot on every connect) |

Full, interactive list: http://127.0.0.1:8000/docs.

## Tests

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pytest -q                       # unit tests, no services needed
python scripts/demo_run.py                # end-to-end check against the running services
python scripts/fake_devices.py -n 50 --mssv-from-class 1 --test all --drive   # simulated class + report
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `start.ps1` cannot be loaded / is not digitally signed | `Unblock-File .\start.ps1, .\stop.ps1` |
| "Docker is not running" | Open Docker Desktop and wait for "Engine running" |
| Page behaves like an old version | Open http://127.0.0.1:8000/ (not `/ui/`), or Ctrl+F5 once |
| AI step missing, or `/health` shows `"analyze": false` | Add `GEMINI_API_KEY` to `.env`, run `.\start.ps1` again |
| Fewer simulated clickers than `-Fake` | The class list has fewer students than requested (the fake devices window prints a warning) |
| Real clicker and a fake device share a number | Start with `-PicoIds <number>` |
| Device gets `NOT_IN_CLASS` | MSSV is not on the class list linked to the session; upload the list before opening the board |
| Phones cannot reach the laptop | Once, as administrator: `.\start.ps1 -OpenFirewall`; Wi-Fi set to Private |
| BLE problems (clicker not found, phone mode `ABORTED`) | See `BLE_BRIDGE.md` and `python scripts/ble_bridge.py --check` |

## More documentation

- `WALKTHROUGH.md`: how the code works, file by file
- `DECISIONS.md`: design decisions and the alternatives rejected
- `BLE_BRIDGE.md`: real clickers, phone mode, broken-key option
- `TESTING_DEVICES.md`: simulated devices, scenarios, phone clickers
- `sql/README_SQL.md`: reference SQL for practice
- `web/INTEGRATION_GUIDE.md`, `web/BACKEND_API.md`: how the teacher page uses the API
