# Testing without the real clickers

Two tools, both talk **MQTT with exactly the device contract**, so the backend cannot tell them from Picos:

| Tool | What it is | Use it for |
|---|---|---|
| `scripts/fake_devices.py` | Up to 60 simulated clickers with BLE delay, packet loss, resends and test scenarios | Load, timing, error cases, automatic PASS/FAIL report |
| `web/phone.html` | A phone becomes one clicker (MSSV entry + A B C D buttons) | Live demos, testing by hand, showing the system to the team |

---

## 1. Fake devices

Run from `backend`, `.venv` active, with Docker running.

```powershell
# follow the teacher page (click through the quiz in the browser; a report prints when the session ends)
python scripts/fake_devices.py -n 60 --mssv-from-class 1 --test all

# no browser: the script creates a short quiz + session, runs it, prints the report, exit code 0 = PASS
python scripts/fake_devices.py -n 60 --mssv-from-class 1 --test all --drive
```

`--drive` ends whatever session is running in the room first. Do not use it while a real class is running.

### Scenarios (`--test`)

| Name | What the devices do | Expected acks |
|---|---|---|
| `normal` | Random reaction times and order; 10 % change their mind; 8 % never answer | OK |
| `burst` | All devices register at the same instant and answer at the same instant (no BLE delay) | OK, all within a few hundred ms |
| `lossy` | 20 % of BLE packets lost both ways; resend with the same `seq` after 1.5 s | OK, or DUP on a resend (= already stored) |
| `wrong_mssv` | 7-digit MSSV, MSSV not in the class, or another student's MSSV, then the student corrects it | BAD / NOT_IN_CLASS / DUP, then OK |
| `reboot` | Clicker reboots mid-quiz (`seq` back to 1); half re-register, half do not | OK if re-registered; DUP if not (**known issue, round 2**) |
| `late` | Answers after the deadline, and one answer for the wrong question | CLOSED |
| `dropout` | Device disappears for some questions, then comes back | OK |
| `spam` | 3–6 presses quickly; BLE may deliver them out of order | OK / DUP; the database keeps the last press |
| `random` | One of the above, picked at random | |
| `all` | Devices split across all scenarios in one session | |

### Useful options

| Option | Default | Meaning |
|---|---|---|
| `-n` | 30 | number of devices (max 60) |
| `--ble-ms` | 40 | median BLE delay per hop, ms (`0` = no BLE) |
| `--ble-interval` | 30 | extra 0..N ms waiting for the next BLE connection event |
| `--ble-loss` | 0.02 | share of BLE packets lost per hop |
| `--ack-timeout`, `--tries` | 1.5 s, 3 | when and how often a device resends |
| `--seed` | none | same number = same random choices (repeat a failing run) |
| `--questions`, `--q-time`, `--reg-wait` | 3, 8 s, 6 s | `--drive` only |
| `--port` | 1884 | broker port on the laptop |

### Reading the report

```
scenario    dev  msgs retry      OK     DUP  CLOSED  NOT_IN     BAD  UNREGI  NO_ACK  wrong
...
Latency press -> ack on the device (BLE up + server + BLE down), first try:  p50 / p95 / p99 / max / over 1 s
Server only (MQTT publish -> ack on MQTT)
Burst answer: 60 sent at the same instant -> all acked within ... ms
Integrity: N device x question answers checked against the database, 0 mismatch(es)
RESULT: PASS
```

- **wrong** = acks that are not what the scenario expects. Must be 0.
- **Latency** is the number for the "< 1 s with 60 devices" requirement. It includes a *simulated* BLE link; the real BLE → gateway → bridge time must be measured again with the hardware.
- **Integrity** compares, for every device and question, the student's last confirmed press with what the database stored. Must be 0 mismatches.
- **Known issue** lines are expected until round 2 (reboot without re-registering).

### What it found (already fixed)

1. A resend after a lost ack, arriving after the deadline, was answered `CLOSED` although the first copy was stored. Now `DUP` is checked before `CLOSED` (DECISIONS 21).
2. A burst of 60 acks queued behind the MQTT "in flight" limit of 20. Raised to 100 (DECISIONS 22).
3. A server restart left an open question open forever (timers live in memory). The timers are now re-created at startup.

---

## 2. Phones as clickers

The backend serves `phone.html`; the phone connects to Mosquitto's WebSocket port **9001** (already open in `docker-compose.yml`). No app to install, no internet needed.

1. Laptop and phones on the **same Wi-Fi**. School Wi-Fi often blocks device-to-device traffic: use a phone hotspot or a small router.
2. Laptop IP: `ipconfig` → "IPv4 Address" of the Wi-Fi adapter, e.g. `192.168.1.23`.
3. Allow ports 8000 and 9001 through the Windows firewall (once, PowerShell **as administrator**):
   ```powershell
   New-NetFirewallRule -DisplayName "ARS 8000 9001" -Direction Inbound -Protocol TCP -LocalPort 8000,9001 -Action Allow -Profile Private
   ```
   The Wi-Fi network must be set to **Private** in Windows settings.
4. On each phone open `http://192.168.1.23:8000/ui/phone.html?dev=51` — each phone a **different** `dev`
   (e.g. fake devices `-n 50`, phones 51–60). The number can also be changed under "Cài đặt thiết bị".
5. Teacher starts the session → phone shows the MSSV screen → enter an MSSV from the class list → A B C D during questions.
   The phone shows the server's answer and the round-trip time in ms.

Phones and fake devices work together in the same session. "Mô phỏng khởi động lại" sets `seq` back to 0, like a rebooted Pico.

If the phone says "mất kết nối": check the IP, the firewall rule, and that `docker compose ps` shows mosquitto Up.
