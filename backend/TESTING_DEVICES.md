# Running and testing everything (without the real clickers)

All commands are **PowerShell**, in the `backend` folder, unless stated otherwise.
Repo folder: `C:\Users\doann\Documents\IOT26\IoT2026-Audience-Response-System`

Two test tools, both speak **MQTT with exactly the device contract**, so the backend cannot tell them from Picos:

| Tool | What it is | Use it for |
|---|---|---|
| `scripts/fake_devices.py` | Up to 60 simulated clickers with BLE delay, packet loss, resends and test scenarios | Load, timing, error cases, automatic PASS/FAIL report |
| `web/phone.html` | A phone becomes one clicker (MSSV entry + A B C D) | Live demos, testing by hand, showing the team |

```
fake_devices.py ─┐                                                   ┌─ teacher page  http://127.0.0.1:8000/ui/
phones (ws:9001) ─┼─ MQTT ─► Mosquitto ◄─ MQTT ─ backend (FastAPI) ─┤
real Picos later ─┘          (Docker)            + PostgreSQL        └─ WebSocket live updates
```

---

## 1. One-time setup

1. Docker Desktop → Settings → General → tick **Start Docker Desktop when you sign in**.
2. Allow PowerShell scripts: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`
3. Python venv (only for the test scripts; the server itself runs in Docker):
   ```powershell
   cd C:\Users\doann\Documents\IOT26\IoT2026-Audience-Response-System\backend
   python -m venv .venv
   .venv\Scripts\activate
   python -m pip install -r requirements.txt
   ```
4. Build and start, then create the tables and the demo data (quiz, devices 1–60, class `DH22CNTT01` with 60 students):
   ```powershell
   docker compose up -d --build
   docker compose exec db psql -U ars -d ars -f /sql/schema.sql
   docker compose exec db psql -U ars -d ars -f /sql/seed.sql
   docker compose restart api
   ```
   `schema.sql` **deletes all data**. Only run it the first time, or when you want a clean database.
5. For phones only: section 6, steps 1–3.

> Class list still has only 10 students (old seed)? Add 11–60 without deleting anything:
> ```powershell
> docker compose exec db psql -U ars -d ars -c "INSERT INTO class_students (class_id, mssv, full_name) SELECT 1, '221340' || lpad(n::text, 2, '0'), 'Student ' || n FROM generate_series(11, 60) AS n ON CONFLICT DO NOTHING;"
> ```

---

## 2. Every day: start

```powershell
cd C:\Users\doann\Documents\IOT26\IoT2026-Audience-Response-System
git switch khoa
git pull
cd backend
docker compose up -d --build      # db + mosquitto + api
docker compose ps                 # 3 services "Up"
.venv\Scripts\activate
```

Checks:
- http://127.0.0.1:8000/health → `{"db":"ok","mqtt":"connected"}`
- `python -m pytest -q` → `51 passed`
- `python scripts/demo_run.py` → `41/41 checks passed`

After a change in `app/` or `sql/queries.sql`: `docker compose up -d --build`.
After a change in `web/` (page, phone page): just refresh the browser.

---

## 3. Windows layout for a full test

| Window | Command | What you see |
|---|---|---|
| Browser | http://127.0.0.1:8000/ui/ (login `GV001` / `123456`) | Teacher page |
| PowerShell 1 | `python scripts/fake_devices.py -n 50 --mssv-from-class 1 --test all` | Fake clickers + report at the end |
| PowerShell 2 (optional) | `docker compose exec mosquitto mosquitto_sub -t "ars/#" -v` | Every MQTT message |
| PowerShell 3 (optional) | `docker compose logs -f api` | Backend log |
| Phones (optional) | `http://<laptop IP>:8000/ui/phone.html?dev=51` … `?dev=60` | Phone clickers |

`-n 50` leaves device numbers 51–60 (and MSSVs 22134051–22134060) free for the phones. Without phones use `-n 60`.

---

## 4. Run A — a full quiz with the teacher page (by hand)

1. Start the windows of section 3 (fake devices first or after, both work: they follow the session state).
2. Teacher page → log in → **Thiết lập**: class `DH22CNTT01` shows "có danh sách trên máy chủ".
   Lower the question times if you want a quick run.
3. **Sang giao diện kiểm tra →**: a new session opens; the board fills with devices within a few seconds.
4. **Bắt đầu** → questions open; the counter "Tín hiệu đã nhận" goes up; the server closes each question at its deadline.
5. After the last question → **Tổng kết** / **Tổng quan**.
6. PowerShell 1 prints the **TEST REPORT** (section 7). The fake devices then wait for the next session; **Hủy** or a new quiz starts another run. Ctrl+C to stop.

Try during the run: reload the browser in the middle of a question (log in → "Nối lại"), or **Hủy** (all devices must register again).

---

## 5. Run B — automatic tests, no browser (`--drive`)

The script creates a short quiz (3 questions × 8 s) and a session itself, runs it, prints the report and exits with code 0 (PASS) or 1 (FAIL). About 40 s per run.

```powershell
# one scenario
python scripts/fake_devices.py -n 60 --mssv-from-class 1 --test all --drive

# every scenario, one after the other, with a summary
foreach ($t in "normal","burst","lossy","wrong_mssv","reboot","late","dropout","spam","all") {
  python scripts/fake_devices.py -n 60 --mssv-from-class 1 --test $t --drive
  if ($LASTEXITCODE -eq 0) { Write-Host "PASS  $t" -ForegroundColor Green } else { Write-Host "FAIL  $t" -ForegroundColor Red }
}

# harsher radio: 10 % packet loss, 80 ms BLE delay
python scripts/fake_devices.py -n 60 --mssv-from-class 1 --test all --drive --ble-loss 0.1 --ble-ms 80

# repeat exactly the same random choices (to re-check a failing run)
python scripts/fake_devices.py -n 60 --mssv-from-class 1 --test all --drive --seed 7
```

`--drive` **ends whatever session is running in the room** first. Don't use it while the teacher page is running a quiz in the same room (or use another room: `--room room2`).

---

## 6. Run C — phones as clickers

The backend serves `phone.html`; the phone connects to Mosquitto's WebSocket port **9001** (already open in `docker-compose.yml`). No app, no internet needed.

1. Laptop and phones on the **same Wi-Fi**. School Wi-Fi often blocks device-to-device traffic: use a phone hotspot or a small router.
2. Laptop IP: `ipconfig` → "IPv4 Address" of the Wi-Fi adapter, e.g. `192.168.1.23`.
3. Once, PowerShell **as administrator**, open the two ports (the Wi-Fi must be set to **Private** in Windows settings):
   ```powershell
   New-NetFirewallRule -DisplayName "ARS 8000 9001" -Direction Inbound -Protocol TCP -LocalPort 8000,9001 -Action Allow -Profile Private
   ```
4. Each phone opens `http://192.168.1.23:8000/ui/phone.html?dev=51` — a **different** `dev` per phone (51–60 when fake devices use 1–50). The number can be changed later under "Cài đặt thiết bị".
5. Run a quiz as in Run A. On the phone: MSSV screen → enter an MSSV from the class (e.g. `22134051`) → during a question pick A–D, then **Xác nhận**. The answer shows **Đã khoá** only when the server confirms it (with the locked letter from the ack); otherwise **Chưa khoá đáp án** with the reason. The phone also shows the round-trip time in ms.

"Mô phỏng khởi động lại" sets the phone's `seq` back to 0, like a rebooted Pico (try it to see the round-2 known issue).

---

## 7. Scenarios and reading the report

| `--test` | What the devices do | Expected acks |
|---|---|---|
| `normal` | Random reaction times and order; 8 % never answer; 10 % try to change after the lock | OK; LOCKED for the change attempt |
| `burst` | All register at the same instant and answer at the same instant (no BLE delay) | OK, all within a few hundred ms |
| `lossy` | 20 % of BLE packets lost both ways; resend with the same `seq` after 1.5 s | OK (the server confirms a resend of the locked answer with OK again) |
| `wrong_mssv` | 7-digit MSSV, MSSV not in the class, or another student's MSSV, then corrected | BAD / NOT_IN_CLASS / DUP, then OK |
| `reboot` | Reboot mid-quiz (`seq` back to 1); half re-register, half do not | OK if re-registered; DUP if not (**known issue, round 2**) |
| `late` | Answers after the deadline, and one for the wrong question | CLOSED |
| `dropout` | Device disappears for some questions, then comes back | OK |
| `spam` | Confirm pressed 3–6 times quickly; BLE may deliver them out of order | one OK, the rest LOCKED; all acks name the same locked choice |
| `random` | One of the above, picked at random | |
| `all` | Devices split across all scenarios in one session | |

| Option | Default | Meaning |
|---|---|---|
| `-n` | 30 | number of devices (max 60) |
| `--mssv-from-class` | none | use the MSSVs of this class id (needed when the session checks a class list) |
| `--ble-ms` / `--ble-interval` | 40 / 30 | median BLE delay per hop (ms, `0` = off) / extra wait for the next BLE connection event |
| `--ble-loss` | 0.02 | share of BLE packets lost per hop |
| `--ack-timeout` / `--tries` | 1.5 s / 3 | when and how often a device resends |
| `--seed` | none | same number = same random choices |
| `--room` / `--port` | room1 / 1884 | MQTT room and broker port on the laptop |
| `--questions` / `--q-time` / `--reg-wait` | 3 / 8 s / 6 s | `--drive` only |

```
scenario    dev  msgs retry      OK  LOCKED     DUP  CLOSED  NOT_IN     BAD  UNREGI  NO_ACK  wrong
...
Latency press -> ack on the device (BLE up + server + BLE down), first try:  p50 / p95 / p99 / max / over 1 s
Server only (MQTT publish -> ack on MQTT)
Burst answer: 60 sent at the same instant -> all acked (first try) within ... ms
Integrity: N device x question answers checked against the database, 0 mismatch(es)
RESULT: PASS
```

- **wrong** = acks the scenario did not expect. Must be 0.
- **Latency** is the number for "< 1 s with 60 devices". It includes a *simulated* BLE link; measure again with the real BLE → gateway → bridge.
- **Integrity** = for every device and question, every ack (OK / LOCKED) names the same locked choice, that choice is what the database stored, and only one message was accepted. Must be 0 mismatches.
- **Known issue** lines are expected until round 2 (reboot without re-registering).

Results on the development machine (60 devices, BLE 40 ms, 2 % loss): p50 ≈ 170 ms, p95 ≈ 250 ms, max < 400 ms; 60 simultaneous answers all acked within ≈ 300 ms.

What the simulator found (fixed): a late resend got `CLOSED` although already stored (now `OK` again: the server confirms its lock); 60 acks queued behind an MQTT in-flight limit of 20 (now 100); a server restart left an open question open forever (timers now resume at startup).

---

## 8. Stop

```powershell
# Ctrl+C in the fake_devices / logs windows
docker compose stop          # data is kept; "docker compose down -v" would DELETE the database
cd ..
git status                   # .env must NOT be listed
git add .
git commit -m "test: ..."
git push
```

---

## 9. Problems

| Symptom | Fix |
|---|---|
| `WARNING: ... the class list has only N students` | Class list too short: the SQL in section 1 (note) adds students 11–60 |
| Report: many `NOT_IN` / devices refused | Fake MSSVs not on the class list: add `--mssv-from-class 1` |
| `--drive`: `cannot end session ...` | `docker compose restart api`, then run again |
| `409 room ... already has an active session` (page) | The page ends old sessions itself; if not, `docker compose restart api` and retry |
| Fake devices print nothing after `state: ...` | Wrong broker port: Docker publishes **1884** (`--port 1884` is the default) |
| Page board stays empty | Fake devices in another room (`--room`), or the session was not started yet |
| `Connection refused` / `ConnectError` | `docker compose ps`: start Docker Desktop, `docker compose up -d` |
| Phone: "mất kết nối" | Wrong IP, firewall rule missing, Wi-Fi not "Private", or school Wi-Fi isolation (use a hotspot) |
| Phone: `MSSV không có trong lớp` | Use an MSSV from the class list (`22134001`–`22134060`) |
| Phone: `thiết bị chưa đăng ký` | The page was opened after registration: "Gửi lại MSSV" under the buttons |
| Two phones fight over one number | Each phone a different `?dev=` |
| Report latency much higher than usual | Laptop busy (antivirus, many browser tabs) or Wi-Fi; compare "Server only" with the full latency |
| `Psycopg cannot use the 'ProactorEventLoop'` | Only when running uvicorn on Windows; the scripts already handle it. Use Docker for the server |
