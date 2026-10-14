# Connecting the teacher page (Phòng Thi Clicker) to the backend

For the page owner. Everything here was tested against the running backend
(`ars-client.js`: 15/15 checks, backend demo: 39/39 checks).

---

## 1. Setup (5 minutes)

1. Put the page in `backend/web/` and name it **`index.html`**. `ars-client.js` is already there.
2. In `index.html`, add this line **before** the page's own `<script>` (after the SheetJS script):
   ```html
   <script src="ars-client.js"></script>
   ```
3. Start (first time, or after backend changes): `docker compose up -d --build`.
   The `web` folder is shared with the container: **after editing `index.html`, just refresh the browser.**
4. Open **http://127.0.0.1:8000/ui/** — the backend serves the page, so API calls and the
   live connection work with no CORS setup.
5. For devices, open another terminal (backend folder, `.venv` active):
   `python scripts/fake_devices.py -n 40 --mssv-from-class 1` — 40 fake clickers that register
   and answer through the real backend, exactly like the Picos will.

---

## 2. Conventions

| Page | Backend / devices | Helper |
|---|---|---|
| Answers `A B C D` | `1 2 3 4` | `ARS.letter(2) → "B"`, `ARS.choice("B") → 2` |
| Question index `0..N-1` | Position `1..N` | index = position − 1 |
| Device id (random hex now) | Device number `1..60`, shown as `"07"` | `pageDevices()` returns `id: "07"` |
| `S.questions` `{q, o[4], a, time, delay}` | quiz with `options`, `correct_choice`, `time_limit_s`, `review_delay_s` | `saveQuiz()`, `toPageQuestions()` |
| Timer decides the end | **The server decides the end** (deadline); the page only displays it | `onState` gives `deadlineAt` |

Every `ARS` call returns a Promise; on an error it throws with the backend's explanation in
`err.message` (e.g. `'next' is not allowed in phase IDLE`) — show it with `toast(err.message)`.

---

## 3. Replace the mock, function by function

| Page code today | Replace with |
|---|---|
| `Server.saveExam(...)` (button *Lưu lên database*) | `const quiz = await ARS.saveQuiz({title: $('#examName').value, subject: $('#subj').value, questions: S.questions}); S.examId = quiz.id;` |
| *(new)* class list upload in *Thiết lập* | parse with SheetJS as for questions → `const {students, bad} = ARS.parseStudentRows(rows); const cls = await ARS.uploadClass({code: $('#cls').value, name: $('#cls').value, students}); S.classId = cls.id;` |
| `#toBoard` → `show('s3'); startJoin()` | `S.session = await ARS.createSession({quizId: S.examId, room: 'room1', classId: S.classId}); await ARS.start(S.session.id); S.live = ARS.live(S.session.id, handlers); show('s3');` (save the quiz first if `S.examId` is empty) |
| `startJoin()` / `addDevice()` random devices | **delete** — devices arrive through `onDevice` (below) |
| `bStart` → `startQuestion(0)` | `await ARS.next(S.session.id)` — the screen changes when `onState` says `QUESTION_OPEN` |
| `startQuestion(i)` timer `setInterval(tickQ)` | keep the drawing, but compute seconds left from the server: `S.left = Math.ceil((S.deadlineAt - Date.now())/1000)` |
| `simAnswers()` | **delete** — answers arrive through `onAnswer` |
| `tickQ()` → `if (S.left<=0) endQuestion()` | remove the `endQuestion()` call: the server closes the question and `onState` brings `QUESTION_CLOSED` |
| `#qEnd` *Kết thúc câu* | `await ARS.close(S.session.id)` |
| `next()` after the review delay | `await ARS.next(S.session.id)`; on the last question call `await ARS.end(S.session.id)` then show the summary |
| `endQuestion()` shows the board with results | on `QUESTION_CLOSED`: `S.devices = await ARS.pageDevices(S.session.id)` (real letters), then `renderBoard()` |
| `renderSummary()` / `scoreOf()` | can stay (it uses `S.devices`), or use `ARS.results(id)` → `ranking`, `questions`, `distribution` |
| `#qPause` *Dừng* during a question | **not in the backend yet (round 2)** — hide or disable for now |
| `cancelExam()` *Hủy* | for now: `await ARS.end(id)`, then create a new session — registrations start empty (fixes V1 item 1). A proper "cancel" comes in round 2 |
| `complaint()` / edit correct answer + regrade | UI can stay, but **not saved on the server yet (round 2: `answer_overrides`)** — mark it "chưa lưu" |
| `Server.login` | keep as is for the demo (it is not real security) |
| `SLOTS = 50` | `60` — and make the board 3 × 20 |

---

## 4. The live handlers (the core of the integration)

```js
const handlers = {
  // on every (re)connect: rebuild the board from the server's truth
  onSnapshot: ({session, devices}) => {
    S.devices = devices.map(d => ({id: String(d.device_no).padStart(2,'0'), dev: d.device_no,
                                   mssv: d.mssv, name: d.full_name || '', ans: {}, fix: {}}));
    if (session.deadlineAt) S.deadlineAt = session.deadlineAt;
    renderBoard();
  },
  onDevice: d => {                                   // a clicker registered
    if (!S.devices.some(x => x.dev === d.dev))
      S.devices.push({id: String(d.dev).padStart(2,'0'), dev: d.dev, mssv: d.mssv,
                      name: d.full_name || '', ans: {}, fix: {}});
    if (S.screen === 's3') renderBoard();
  },
  onAnswer: a => {                                   // the choice is NOT sent while open
    const d = S.devices.find(x => x.dev === a.dev);
    if (d) d.ans[a.q - 1] = '✓';                      // placeholder; real letter after close
    $('#recvN').textContent = a.answered;             // live counter on the question screen
  },
  onState: async s => {
    if (s.phase === 'QUESTION_OPEN') { S.cur = s.q - 1; S.deadlineAt = s.deadlineAt; startQuestion(S.cur); }
    if (s.phase === 'QUESTION_CLOSED') { S.devices = await ARS.pageDevices(S.session.id); endQuestion(); }
    if (s.phase === 'ENDED') { S.phase = 'done'; show('s5'); }
  },
  onReject: r => {                                   // tell the teacher why a device was refused
    const why = {NOT_IN_CLASS: 'MSSV không có trong danh sách lớp', DUP: 'MSSV đã dùng trên thiết bị khác',
                 BAD: 'dữ liệu sai', CLOSED: 'ngoài thời gian nhận', UNREGISTERED: 'thiết bị chưa đăng ký'};
    toast(`Thiết bị ${String(r.dev).padStart(2,'0')}: ${why[r.code] || r.code}`);
  },
  onConnection: ok => { if (!ok) toast('Mất kết nối máy chủ, đang kết nối lại…'); },
};
```

Note: `startQuestion()` / `endQuestion()` today also start timers and switch screens. Split them so
that **the server's events drive the screen**, and the page's own timer only draws the countdown.

---

## 5. What the page must NOT do any more

- Decide that a question is over (the server's deadline does).
- Invent devices or answers (only the "simulate" script does, through the backend).
- Keep answers only in the browser: after a reload, `onSnapshot` + `pageDevices()` restore the board.

---

## 6. Status of the V1 list

| # | Item | Status |
|---|---|---|
| 1 | Clear MSSVs when a test is cancelled/ended | **Done by design**: registrations belong to a session; a new session starts empty |
| 2 | Teacher corrects a student's answer | Round 2 (`answer_overrides` with audit trail) |
| 3 | Flag to lock/accept device signals | Round 2 (paused state) — the phases already block answers outside `QUESTION_OPEN` |
| 4 | Score distribution | **Done in the backend**: `ARS.results(id).distribution` → draw bars |
| 5 | Reconnect/disconnect | Backend: a device may re-register itself during the quiz; page: `ARS.live` reconnects and `onSnapshot` rebuilds. Remaining `seq` edge case: round 2 |
| 6 | Separate student-facing screen | Round 2 (projector page using the same live events) |
| 7 | "Dừng" → stop answers and go back to MSSV registration | Round 2 (new state change) |
| 8 | Check MSSV against the class list | **Done**: upload the class, pass `classId` to `createSession`; wrong MSSV → device ack `NOT_IN_CLASS` + `onReject` on the page |

**Contract change for the device team:** new ack code `NOT_IN_CLASS`. The broker is on host
port **1884** (1883 is taken by a Windows Mosquitto service).
