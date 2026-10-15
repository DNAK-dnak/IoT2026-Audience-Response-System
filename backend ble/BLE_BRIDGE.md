# BLE bridge: real clickers → backend

`scripts/ble_bridge.py` = the hardware team's `ble_receiver.py` + MQTT. Their BLE protocol is unchanged
(`PicoRemote_<dev>`, service `0x1234`, TX `0x5678` notify, RX `0x9abc` write, `MODE_1/2/3`, `UNLOCK`).

```
PicoRemote_7 ──BLE──► laptop: ble_bridge.py ──MQTT──► Mosquitto ──► backend + database ──► teacher page
             ◄─MODE_x / ACK─                  ◄─state / ack─
```

## Run (Windows laptop with Bluetooth)

```powershell
cd backend
.venv\Scripts\activate
python -m pip install -r requirements-bridge.txt     # once
docker compose up -d                                 # backend must be running
python scripts/ble_bridge.py                         # -v for every command sent to the clickers
```
Then the teacher page as usual: http://127.0.0.1:8000/ui/. Clickers and fake devices can run together:
fake devices `-n 50` (devices 1–50), real clickers numbered 51–60.

## What the bridge does

| Backend | → command written to the clicker |
|---|---|
| no session / IDLE / ENDED | `{"cmd":"MODE_1"}` |
| REGISTER | `{"cmd":"MODE_2"}`; after the MSSV is accepted `{"cmd":"MODE_1"}` |
| QUESTION_OPEN (registered clickers only) | `{"cmd":"MODE_3","q":2,"n":4,"deadline_ms":19980}` |
| QUESTION_CLOSED | `{"cmd":"MODE_1"}` |
| every ack | `{"cmd":"ACK","kind":"answer","seq":12,"code":"OK","q":2,"choice":4}` |
| answer refused while still open (BAD) | + `{"cmd":"UNLOCK"}` |
| register refused (NOT_IN_CLASS, DUP, BAD) | + `{"cmd":"MODE_2"}` (type again) |
| answer `DUP` (clicker rebooted, seq behind) | + `{"cmd":"MODE_2"}` (re-enter the same MSSV, then back to MODE_3) |

When a clicker (re)connects it gets the right mode at once, using the backend's real registration list.
`--fill-q` (on by default): the current firmware always sends `"q":1`; the bridge replaces it with the open question.

Tested without Bluetooth: a copy of `mock_pico.py`'s behaviour driven through the bridge against the
running backend — 15/15 checks (modes, wrong MSSV, lock + ACK, keys locked, q filled in, reconnect
recovery, LOCKED, database content).

## Gửi nhóm phần cứng (cần làm trên Pico)

1. **Đọc `ACK`**: chỉ hiện "đã khoá" khi nhận `{"cmd":"ACK","kind":"answer","code":"OK" hoặc "LOCKED","choice":N}` — hiện đúng đáp án `choice` từ máy chủ, không tự khoá theo phím đã bấm. Mã khác → hiện "chưa khoá" (+ lý do theo `code`).
2. **Đọc `q` trong `MODE_3`** và gửi lại đúng `q` đó trong đáp án (khi xong, chạy bridge với `--no-fill-q`).
3. **Kích thước tin nhắn**: lệnh `ACK`/`MODE_3` dài ~50–70 byte, tin đăng ký ~60 byte. BLE mặc định chỉ 20 byte/gói → trên Pico cần **tăng buffer của characteristic RX** (MicroPython: `gatts_set_buffer(handle, 128, True)`) và **trao đổi MTU** lớn hơn (vd. 128+). Nếu không, tin bị cắt và JSON hỏng.
4. **Khởi động lại**: `seq` tiếp tục tăng (lưu flash) hoặc, nếu về 0, đăng ký lại MSSV khi nhận `MODE_2`.
5. Giữ nguyên: tên `PicoRemote_<số>`, UUID, `MODE_1/2/3`, `UNLOCK`.

## Phone test over Bluetooth (`--phone` + `web/phone_ble.html`)

A phone browser can only **connect to** a Bluetooth device, it cannot **be** one. So for this test the
laptop plays the device side (with `bless`, like `mock_pico.py`) and the phone connects to it. Same
service `0x1234`, same characteristics, same JSON and commands; every command also carries `"dev"`
so several phones can share the connection point.

```
phone (phone_ble.html) ──Bluetooth: write 0x5678──► laptop: ble_bridge.py --phone ──MQTT──► backend
                       ◄──Bluetooth: notify 0x9abc── (MODE_x, ACK)
```

**Laptop (once):** `python -m pip install bless` (plus `requirements-bridge.txt`). Bluetooth on.
**Run:** `docker compose up -d`, then `python scripts/ble_bridge.py --phone -v`
(it can run at the same time as the normal Pico bridge).

**Phone (Android + Chrome; on iPhone use the Bluefy browser):**
1. The page must first be **loaded** once from the laptop over Wi-Fi/hotspot:
   `http://<laptop IP>:8000/ui/phone_ble.html?dev=57` (firewall rule for port 8000 as in TESTING_DEVICES.md).
   After loading, everything goes over Bluetooth.
2. Chrome only allows Bluetooth on secure pages. Once: open `chrome://flags/#unsafely-treat-insecure-origin-as-secure`,
   add `http://<laptop IP>:8000`, set **Enabled**, tap **Relaunch**.
3. Tap **Kết nối Bluetooth** → pick the laptop in the list (it may show the laptop's own name) → the phone
   follows the session: MSSV → A–D → **Xác nhận** → "Đã khoá" only from the server's ACK.
4. Each phone a different `?dev=` (51–60 if fake devices use 1–50) and an MSSV from the class list.

Tested without real Bluetooth: the real page in a browser with a stand-in for the phone's Bluetooth,
wired to the real bridge and backend — 13/13 checks (connect by service, modes, wrong MSSV, select ≠ send,
lock from ACK, LOCKED, closed screen, question 2, database content). Not tested: the radio itself
(Windows advertising with bless, Chrome's Bluetooth chooser) — try it once early.

If `--phone` fails at start: the laptop's Bluetooth adapter may not support acting as a device
(peripheral role). Try another laptop or a USB Bluetooth adapter.

## Note for the hardware team

`mock_pico.py` calls `add_new_characteristic(service, char, properties, permissions, value)`, but bless
expects `(service, char, properties, value, permissions)`. It may still work by luck; swap the last two.

## Limits to know

- Each clicker keeps a BLE **connection** to the laptop. A Windows Bluetooth adapter usually handles only a
  handful at once (often under 10, depends on the adapter). Enough for the demo with a few real clickers;
  60 needs the fake devices (or a different radio design).
- The bridge runs on the laptop itself (Bluetooth is not available inside Docker).
