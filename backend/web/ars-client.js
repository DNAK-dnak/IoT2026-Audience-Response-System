/* =====================================================================
   ars-client.js — connects the teacher page to the ARS backend.

   Load it in the page BEFORE the page's own <script>:
       <script src="ars-client.js"></script>
   It creates one global object: ARS.

   The page is served by the backend itself (http://<server>:8000/ui/),
   so every call below goes to the same server: no CORS, no config needed.

   Conventions:
   - Answers are numbers 1..4 in the backend and on the devices.
     The page shows letters A..D.  ARS.letter(2) === "B",  ARS.choice("B") === 2.
   - Questions are numbered 1..N in the backend (position).
     The page uses indexes 0..N-1.  question index = position - 1.
   - Every function returns a Promise. On an API error it throws an Error whose
     .message is the backend's explanation (show it in a toast), and .status is the HTTP code.
   ===================================================================== */
(function () {
  "use strict";

  const LETTERS = ["A", "B", "C", "D"];
  let BASE = "";                       // "" = same server that served the page

  // ------------------------------------------------------------------ helpers
  async function api(method, path, body) {
    const res = await fetch(BASE + path, {
      method,
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    const text = await res.text();
    const data = text ? JSON.parse(text) : null;
    if (!res.ok) {
      let msg = data && data.detail;
      if (Array.isArray(msg)) msg = msg.map(e => `${(e.loc || []).slice(1).join(".")}: ${e.msg}`).join("; ");
      const err = new Error(msg || `HTTP ${res.status}`);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  const norm = s => String(s).toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "")
    .replace(/đ/g, "d").replace(/[^a-z0-9]/g, "");

  // ------------------------------------------------------------------ public API
  const ARS = {
    /** Only needed if the page is NOT served by the backend, e.g. ARS.config({base: "http://192.168.1.10:8000"}) */
    config(opts) { if (opts && opts.base !== undefined) BASE = opts.base.replace(/\/$/, ""); },

    letter: n => LETTERS[n - 1] || "",          // 2 -> "B"
    choice: l => LETTERS.indexOf(String(l).toUpperCase()) + 1,   // "B" -> 2 (0 if invalid)

    health: () => api("GET", "/health"),

    // ---------------- teacher login ----------------
    /** {ok:true, user:{id,name}} or {ok:false, reason:"BAD"|"LOCKED"} (HTTP 429 = too many failures from this computer) */
    login: (id, password) => api("POST", "/auth/login", { id, password }),
    /** {ok:true} or {ok:false, reason:"BAD_OLD"|"WEAK"|"LOCKED"} */
    changePassword: (id, oldPw, newPw) =>
      api("POST", "/auth/password", { id, old_password: oldPw, new_password: newPw }),

    // ---------------- quizzes ----------------
    /**
     * Save the page's questions as a quiz.
     * pageQuestions: [{q, o:[4 texts], a:"B", time, delay}]  (the page's S.questions format)
     * returns the saved quiz: {id, title, subject, questions:[{id, position, text, options, correct_choice, ...}]}
     */
    saveQuiz({ title, subject, questions }) {
      return api("POST", "/quizzes", {
        title, subject,
        questions: questions.map(q => ({
          text: q.q,
          options: q.o,
          correct_choice: ARS.choice(q.a),
          time_limit_s: q.time || 20,
          review_delay_s: q.delay ?? 8,
        })),
      });
    },
    listQuizzes: () => api("GET", "/quizzes"),
    getQuiz: id => api("GET", `/quizzes/${id}`),
    /** Backend quiz -> the page's S.questions format */
    toPageQuestions: quiz => quiz.questions.map(q => ({
      q: q.text, o: q.options, a: ARS.letter(q.correct_choice), time: q.time_limit_s, delay: q.review_delay_s,
    })),

    // ---------------- classes (student lists) ----------------
    /**
     * Upload a class list. students: [{mssv:"22134001", full_name:"Nguyen Van A"}]
     * mode "replace" (default) replaces the old list; "append" adds/updates.
     */
    uploadClass: ({ code, name, students, mode = "replace" }) =>
      api("POST", "/classes", { code, name, students, mode }),
    listClasses: () => api("GET", "/classes"),
    classStudents: id => api("GET", `/classes/${id}/students`),
    /**
     * Turn rows from SheetJS (XLSX.utils.sheet_to_json) into students.
     * Accepts column names like: mssv / ma_sv / ma so sinh vien / student_id  and  ho_ten / ho va ten / name.
     * Returns {students, bad: [row numbers that were skipped]}
     */
    parseStudentRows(rows) {
      const idKeys = ["mssv", "masv", "masosinhvien", "studentid", "id"];
      const nameKeys = ["hoten", "hovaten", "ten", "name", "fullname"];
      const pick = (r, keys) => { for (const k of Object.keys(r)) if (keys.includes(norm(k))) return String(r[k]).trim(); return ""; };
      const students = [], bad = [];
      rows.forEach((r, i) => {
        const mssv = pick(r, idKeys).replace(/\D/g, "").padStart(8, "0");
        if (/^[0-9]{8}$/.test(mssv) && mssv !== "00000000") students.push({ mssv, full_name: pick(r, nameKeys) || null });
        else bad.push(i + 2);          // +2 = header row + 1-based
      });
      return { students, bad };
    },

    // ---------------- sessions (one run of a quiz) ----------------
    /** classId optional: when set, devices can only register with an MSSV on that class list */
    createSession: ({ quizId, room = "room1", classId = null }) =>
      api("POST", "/sessions", { quiz_id: quizId, room, class_id: classId }),
    getSession: id => api("GET", `/sessions/${id}`),
    /** The running (not ENDED) session in a room, or null */
    activeSession: (room = "room1") => api("GET", `/sessions/active?room=${encodeURIComponent(room)}`),
    start: id => api("POST", `/sessions/${id}/start`),   // IDLE -> REGISTER (devices may register)
    next: id => api("POST", `/sessions/${id}/next`),     // open the next question (409 when none left)
    close: id => api("POST", `/sessions/${id}/close`),   // stop accepting answers now
    end: id => api("POST", `/sessions/${id}/end`),       // finish the session
    /** End a session from ANY phase: closes an open question / starts an idle one first */
    async finish(id) {
      const s = await ARS.getSession(id);
      if (s.phase === "ENDED") return s;
      if (s.phase === "IDLE") await ARS.start(id);
      if (s.phase === "QUESTION_OPEN") await ARS.close(id).catch(() => {});   // the timer may close it first
      return ARS.end(id);
    },
    devices: id => api("GET", `/sessions/${id}/devices`),
    /** the grid AFTER the teacher's fixes: {device_no, q, choice (null = cleared), device_choice, override_id, reason, ...} */
    answers: id => api("GET", `/sessions/${id}/answers`),
    results: id => api("GET", `/sessions/${id}/results`),

    /** Registration phase only (409 otherwise). The device is told to ask for the MSSV again. */
    removeDevice: (sessionId, dev) => api("DELETE", `/sessions/${sessionId}/devices/${dev}`),
    clearDevices: sessionId => api("DELETE", `/sessions/${sessionId}/devices`),

    /**
     * A complaint / manual entry. data: {dev, mssv, q (1..N), answer:"C"|null, reason, note, manual, by}
     * Stored as a separate record (the device's answer is kept). Idempotent: safe to resend.
     */
    setAnswer: (sessionId, d) => api("PUT", `/sessions/${sessionId}/answers`, {
      dev: d.dev, q: d.q, answer: d.answer || null, mssv: d.mssv || null,
      reason: d.reason || "Khác", note: d.note || null, manual: !!d.manual, by: d.by || null,
    }),
    overrides: id => api("GET", `/sessions/${id}/overrides`),

    /** Save the page's final result (upsert by session: resending replaces it) */
    saveResults: (sessionId, payload) => api("POST", `/sessions/${sessionId}/results`, payload),
    savedResults: id => api("GET", `/sessions/${id}/saved-results`),

    /** AI summary of the result -> {text}. Removed below when the server has no API key. */
    analyze: (sessionId, payload) => api("POST", `/sessions/${sessionId}/analyze`, payload),

    /**
     * Build the page's S.devices array from the backend.
     * returns [{id:"07", dev:7, mssv, name, ans:{0:"B", 2:"A"}, fix:{}}]  (ans keyed by question index)
     */
    async pageDevices(sessionId) {
      const [devs, grid] = await Promise.all([ARS.devices(sessionId), ARS.answers(sessionId)]);
      const byDev = new Map(devs.map(d => [d.device_no, {
        id: String(d.device_no).padStart(2, "0"), dev: d.device_no, mssv: d.mssv, name: d.full_name || "", ans: {}, fix: {},
      }]));
      for (const a of grid) {
        const d = byDev.get(a.device_no);
        if (!d) continue;
        if (a.choice) d.ans[a.q - 1] = ARS.letter(a.choice);          // the answer that COUNTS (after fixes)
        if (a.override_id) d.fix[a.q - 1] = {                         // the teacher's fix, same shape as the page's
          from: a.device_choice ? ARS.letter(a.device_choice) : null, to: a.choice ? ARS.letter(a.choice) : null,
          reason: a.reason, note: a.note, manual: a.manual, by: a.by_teacher, at: Date.parse(a.fixed_at),
        };
      }
      return [...byDev.values()];
    },

    // ---------------- live updates ----------------
    /**
     * Open the live connection for a session. Reconnects by itself.
     * handlers (all optional):
     *   onSnapshot({session, devices, answered})  on every (re)connect: rebuild the board from it
     *   onState({phase, q, n_choices, deadline_ms, deadlineAt})   phase changed; deadlineAt = local ms timestamp
     *   onDevice({dev, mssv, full_name})          a device registered
     *   onAnswer({dev, q, answered})              device dev answered question q; answered = total so far
     *   onReject({dev, kind, code})               a message was refused: NOT_IN_CLASS, DUP, BAD, CLOSED, UNREGISTERED
     *   onRemoved({dev, mssv})                    a device was removed (by this page or another one)
     *   onConnection(isConnected)
     * returns {close()}
     */
    live(sessionId, h = {}) {
      let ws, stopped = false, retry = 1000;
      const url = (BASE || location.origin).replace(/^http/, "ws") + `/sessions/${sessionId}/live`;
      const withDeadline = s => (s.deadline_ms != null ? { ...s, deadlineAt: Date.now() + s.deadline_ms } : s);

      function open() {
        ws = new WebSocket(url);
        ws.onopen = () => { retry = 1000; h.onConnection && h.onConnection(true); };
        ws.onmessage = ev => {
          const e = JSON.parse(ev.data);
          if (e.type === "snapshot") h.onSnapshot && h.onSnapshot({ ...e, session: withDeadline(e.session) });
          else if (e.type === "state") h.onState && h.onState(withDeadline(e));
          else if (e.type === "device") h.onDevice && h.onDevice(e);
          else if (e.type === "answer") h.onAnswer && h.onAnswer(e);
          else if (e.type === "ack") h.onReject && h.onReject(e);
          else if (e.type === "removed") h.onRemoved && h.onRemoved(e);
        };
        ws.onclose = ev => {
          h.onConnection && h.onConnection(false);
          if (stopped || ev.code === 4404) return;          // 4404 = session does not exist
          setTimeout(open, retry);
          retry = Math.min(retry * 2, 8000);                 // back off up to 8 s
        };
      }
      open();
      return { close() { stopped = true; ws && ws.close(); } };
    },
  };

  window.ARS = ARS;

  // The AI step always goes through THIS backend (the API key stays on the server).
  // - The page offers the step only if ARS.analyze exists: it disappears when the server has no key.
  // - Any other script that tries to replace ARS.analyze (e.g. the old snippet that called an
  //   ai_server.py on http://localhost:8787) is ignored, with a warning in the console.
  const backendAnalyze = ARS.analyze;
  let analyzeOn = true;
  Object.defineProperty(ARS, "analyze", {
    configurable: false,
    enumerable: true,
    get: () => (analyzeOn ? backendAnalyze : undefined),
    set: () => console.warn("ARS.analyze is provided by the backend (/sessions/{id}/analyze); the replacement was ignored."),
  });
  ARS.health().then(h => { if (h && h.analyze === false) analyzeOn = false; }).catch(() => {});
})();
