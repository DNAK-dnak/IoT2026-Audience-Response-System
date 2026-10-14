# Audience Response System Backend

FastAPI backend for quiz sessions, PostgreSQL storage, and MQTT clicker messages.

## Start

Open PowerShell in this folder and start the three services:

```powershell
docker compose up -d --build
docker compose ps
```

`db`, `mosquitto`, and `api` should be running. Check the API at:

- Health: http://127.0.0.1:8000/health
- Interactive API: http://127.0.0.1:8000/docs

The API runs in Docker. Use a local Python environment only for tests and the demo:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pytest -q
python scripts/demo_run.py
```

## Initialize a New Database

Do this only for a fresh database. **Running `schema.sql` deletes existing database data.**

```powershell
docker compose exec -T db psql -U ars -d ars -f /sql/schema.sql
docker compose exec -T db psql -U ars -d ars -f /sql/seed.sql
```

## Run a Quiz

Use `/docs` to create a session with an existing quiz, for example `quiz_id: 1`:

1. `POST /sessions` with `{"quiz_id": 1, "room": "room1"}`.
2. `POST /sessions/{id}/start`, then `POST /sessions/{id}/next` to open a question.
3. Connect devices to MQTT at `localhost:1884`.
4. Register with topic `ars/room1/dev/07/register` and payload `{"dev":7,"mssv":"22134007","seq":1}`.
5. Answer with topic `ars/room1/dev/07/answer` and payload `{"dev":7,"q":1,"choice":2,"seq":2}`. The acknowledgment arrives on `ars/room1/dev/07/ack`.
6. Close the question or let its timer expire, then use `next` for another question. Finish with `POST /sessions/{id}/end` and view `GET /sessions/{id}/results`.

Only one active session is allowed per room. Use another room or end its active session first. The broker uses host port `1884` because the Windows Mosquitto service may occupy `1883`; the API connects to the broker internally on `1883`.

## Logs and Stop

```powershell
docker compose logs -f api
docker compose exec mosquitto mosquitto_sub -t "ars/#" -v
docker compose stop
```

Stopping containers preserves database data. `docker compose down -v` removes the database volume and permanently deletes its data.

## Round 1: teacher page integration

- Teacher page: put it in `web/index.html`, open http://127.0.0.1:8000/ui/ (edits show on refresh).
- Page client and wiring: `web/ars-client.js` and `web/INTEGRATION_GUIDE.md`.
- Fake devices for manual testing: `python scripts/fake_devices.py -n 40 --mssv-from-class 1`
- New API: `POST/GET /classes`, `GET /classes/{id}/students`, `GET /sessions/{id}/answers`,
  WebSocket `/sessions/{id}/live`, score distribution in `GET /sessions/{id}/results`.
- New device ack code: `NOT_IN_CLASS` (MSSV not on the class list linked to the session).
- The database schema changed: reload `schema.sql` then `seed.sql` (deletes data).
