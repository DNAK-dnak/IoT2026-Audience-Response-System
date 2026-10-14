# Design decisions

One entry per decision: what, why, and what was rejected. Add your own as you go.

1. **MQTT between devices and backend.** Standard IoT protocol, light for microcontrollers; teams only agree on topics and payloads. Rejected: HTTP from each device (heavier, no retained state, no Last Will).
2. **The server is the only clock.** Devices send no timestamps; `raw_events.received_at` (database `now()`) is compared with `sessions.q_deadline` (also `now()`). Rejected: device timestamps (the Pico has no reliable clock).
3. **Raw layer first, insert-only.** Every message is stored in `raw_events` before validation, even garbage, with its final status. Allows auditing and replay. Rejected: validating first and dropping bad messages (loses evidence).
4. **QoS 1 + our own deduplication by `seq`.** Delivery is at-least-once; duplicates are removed with `seq` and a guarded upsert. Rejected: QoS 2 (heavier on the device and still does not stop repeated button presses).
5. **"Latest answer counts" enforced in SQL.** `ON CONFLICT ... DO UPDATE ... WHERE answers.seq < EXCLUDED.seq`, so an older message can never overwrite a newer one, even under a race.
6. **Composite foreign key `answers (session_id, device_no) -> session_devices`.** The database itself refuses answers from unregistered devices.
7. **Partial unique index: one active session per room.** Ended sessions in the same room are allowed.
8. **State machine in Python AND guards in SQL.** Python gives the teacher a clear 409 message; the `WHERE phase = ...` guards make races harmless.
9. **SQL in `sql/queries.sql`, loaded by name.** All queries readable and testable in one file; no ORM, so the SQL skills are real.
10. **Local Mosquitto for development and demo; HiveMQ for remote tests only.** The BLE gateway plugs into the laptop, and latency is lowest locally. Switching is a `.env` change.
11. **Device numbers 1..n chosen by us (`devices.device_no`).** Printed on the clicker; the student ID lives in `session_devices`, because a clicker belongs to different students in different sessions.
12. **Known limitation:** if a device reboots in the middle of a question, its `seq` restarts at 1, so its next answer to that same question is treated as a duplicate. Re-registering resets `last_seq`; later questions are unaffected.
13. **The teacher page is served by the backend (`/ui/`).** Same origin: no CORS setup, one address for the teacher. The page never talks MQTT; only the backend does.
14. **Live updates over WebSocket, with a snapshot on connect.** A page reload or network drop rebuilds the board from the server's truth instead of losing it.
15. **Answer choices are not sent to the page while a question is open.** The board shows only who has answered; letters appear after the question closes (no hints on a shared screen).
16. **Class lists are uploaded by the teacher (Excel parsed in the page, sent as JSON).** `classes.source` records the origin (`upload`, later `school_sync`), so an automatic sync from a school system can be added as another writer of the same tables.
17. **Wrong MSSV is refused at registration with its own ack code `NOT_IN_CLASS`,** so the device can tell the student exactly what is wrong. Shape errors (`BAD`) are still reported first.
18. **Exactly 4 options (A–D) per question,** stored as JSONB text; answers stay numeric 1–4 on the devices and in the database.
19. **Broker on host port 1884** because a Windows Mosquitto service already uses 1883 on the development laptop. Inside Docker, services still use 1883.

20. **Realistic device simulator with scenarios (`scripts/fake_devices.py --test ...`).** BLE delay and loss on both hops, resends with the same `seq`, random reaction times and order, plus a report with latency percentiles and a database integrity check. Both fixes below were found with it.
21. **DUP is checked before CLOSED for answers.** A resend whose first copy was already stored (ack lost on the radio) can arrive after the deadline; it must get `DUP` ("already have it"), not `CLOSED` ("refused"), or the device shows the student a wrong error.
22. **`max_inflight_messages=100` on the MQTT clients.** The default (20 QoS 1 messages waiting for their PUBACK) made a burst of 60 acks queue up; a classroom burst is up to 60.
23. **Phones as clickers (`/ui/phone.html`).** A web page that speaks MQTT over WebSocket (Mosquitto port 9001) with exactly the device topics and messages. mqtt.js is stored in `web/vendor/` so it works without internet.
