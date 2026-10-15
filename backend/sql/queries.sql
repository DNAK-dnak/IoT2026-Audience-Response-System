-- =====================================================================
-- ARS - the SQL statements the backend runs, written for psycopg 3.
-- %(name)s are NAMED placeholders: pass a dict, e.g.
--   await conn.execute(SQL, {"session_id": 1, "dev": 7})
-- Never build these strings with f-strings.
-- Each block starts with "-- name:" so you can find it quickly.
-- =====================================================================


-- ---------------------------------------------------------------------
-- QUIZZES (REST API)
-- ---------------------------------------------------------------------

-- name: create_quiz            -> returns the new quiz id
INSERT INTO quizzes (course_id, title, subject)
VALUES (%(course_id)s, %(title)s, %(subject)s)
RETURNING id;

-- name: add_question           -> run once per question, in the SAME transaction as create_quiz
INSERT INTO questions (quiz_id, position, text, n_choices, correct_choice, time_limit_s, review_delay_s, options)
VALUES (%(quiz_id)s, %(position)s, %(text)s, %(n_choices)s, %(correct_choice)s, %(time_limit_s)s,
        %(review_delay_s)s, %(options)s);

-- name: get_quiz
SELECT id, course_id, title, subject, created_at
FROM quizzes
WHERE id = %(quiz_id)s;

-- name: list_quizzes
SELECT id, course_id, title, subject, created_at
FROM quizzes
ORDER BY id;

-- name: get_questions
SELECT id, position, text, n_choices, correct_choice, time_limit_s, review_delay_s, options
FROM questions
WHERE quiz_id = %(quiz_id)s
ORDER BY position;


-- ---------------------------------------------------------------------
-- SESSIONS: the state machine. Every UPDATE has a guard on the current
-- phase: 0 rows updated = invalid transition -> the API returns 409.
-- ---------------------------------------------------------------------

-- name: create_session         -> UniqueViolation = room already has an active session -> 409
INSERT INTO sessions (quiz_id, room, class_id)
VALUES (%(quiz_id)s, %(room)s, %(class_id)s)
RETURNING id, phase;

-- name: get_session
SELECT s.id, s.quiz_id, s.class_id, s.room, s.phase, s.current_q, s.q_opened_at, s.q_deadline,
       (SELECT COUNT(*) FROM session_devices sd WHERE sd.session_id = s.id) AS registered,
       (SELECT COUNT(*) FROM questions q WHERE q.quiz_id = s.quiz_id)      AS n_questions
FROM sessions s
WHERE s.id = %(session_id)s;

-- name: start_session          IDLE -> REGISTER
UPDATE sessions SET phase = 'REGISTER'
WHERE id = %(session_id)s AND phase = 'IDLE'
RETURNING id, phase;

-- name: open_next_question     REGISTER | QUESTION_CLOSED -> QUESTION_OPEN (0 rows = wrong phase or no next question)
UPDATE sessions s
SET phase       = 'QUESTION_OPEN',
    current_q   = COALESCE(s.current_q, 0) + 1,
    q_opened_at = now(),
    q_deadline  = now() + make_interval(secs => q.time_limit_s)
FROM questions q
WHERE s.id = %(session_id)s
  AND s.phase IN ('REGISTER', 'QUESTION_CLOSED')
  AND q.quiz_id  = s.quiz_id
  AND q.position = COALESCE(s.current_q, 0) + 1
RETURNING s.id, s.phase, s.current_q, s.q_deadline, q.n_choices, q.time_limit_s;

-- name: close_question         QUESTION_OPEN -> QUESTION_CLOSED, only if THIS question is still open
--                              (the auto-close timer passes the question it was started for)
UPDATE sessions SET phase = 'QUESTION_CLOSED'
WHERE id = %(session_id)s AND phase = 'QUESTION_OPEN' AND current_q = %(q)s
RETURNING id, phase, current_q;

-- name: end_session            REGISTER | QUESTION_CLOSED -> ENDED
UPDATE sessions SET phase = 'ENDED', ended_at = now()
WHERE id = %(session_id)s AND phase IN ('REGISTER', 'QUESTION_CLOSED')
RETURNING id, phase;

-- name: state_message          -> the retained payload for ars/<room>/state
SELECT s.phase,
       s.current_q AS q,
       q.n_choices,
       CASE WHEN s.phase = 'QUESTION_OPEN'
            THEN GREATEST(0, EXTRACT(EPOCH FROM (s.q_deadline - now())) * 1000)::int
       END AS deadline_ms
FROM sessions s
LEFT JOIN questions q ON q.quiz_id = s.quiz_id AND q.position = s.current_q
WHERE s.id = %(session_id)s;

-- name: list_session_devices   -> with the student's name when the session has a class list
SELECT sd.device_no, sd.mssv, cs.full_name, sd.registered_at, sd.last_seq
FROM session_devices sd
JOIN sessions s             ON s.id = sd.session_id
LEFT JOIN class_students cs ON cs.class_id = s.class_id AND cs.mssv = sd.mssv
WHERE sd.session_id = %(session_id)s
ORDER BY sd.device_no;

-- name: session_answers        -> the answer grid: one row per device per answered question
SELECT a.device_no, sd.mssv, q.position AS q, a.choice, (a.choice = q.correct_choice) AS correct,
       a.seq, a.answered_at
FROM answers a
JOIN session_devices sd ON sd.session_id = a.session_id AND sd.device_no = a.device_no
JOIN questions q        ON q.id = a.question_id
WHERE a.session_id = %(session_id)s
ORDER BY a.device_no, q.position;

-- name: answer_count           -> live counter for the open question
SELECT COUNT(*) AS answered
FROM answers a
JOIN sessions s  ON s.id = a.session_id
JOIN questions q ON q.id = a.question_id AND q.quiz_id = s.quiz_id AND q.position = s.current_q
WHERE a.session_id = %(session_id)s;


-- ---------------------------------------------------------------------
-- MQTT WORKER: raw layer first, then rules (in Python), then clean layer
-- ---------------------------------------------------------------------

-- name: find_active_session    -> None = no session in this room (status NO_SESSION)
SELECT id, quiz_id, phase, current_q, q_deadline
FROM sessions
WHERE room = %(room)s AND phase <> 'ENDED';

-- name: insert_raw_event       -> ALWAYS first, even for garbage. payload = Jsonb(obj) or None
INSERT INTO raw_events (topic, device_no, kind, payload_text, payload, session_id)
VALUES (%(topic)s, %(dev)s, %(kind)s, %(payload_text)s, %(payload)s, %(session_id)s)
RETURNING id, received_at;

-- name: answer_context         -> everything rules.check_answer needs, in ONE query
SELECT s.phase,
       s.current_q,
       s.q_deadline,
       q.id              AS question_id,     -- NULL if the quiz has no question with this position
       q.n_choices,
       sd.device_no IS NOT NULL AS registered,
       sd.last_seq,                           -- NULL if not registered
       a.seq             AS stored_seq,       -- the LOCKED answer of this device for this question
       a.choice          AS stored_choice     -- (NULL = not answered yet)
FROM sessions s
LEFT JOIN questions q        ON q.quiz_id = s.quiz_id AND q.position = %(q)s
LEFT JOIN session_devices sd ON sd.session_id = s.id AND sd.device_no = %(dev)s
LEFT JOIN answers a          ON a.session_id = s.id AND a.device_no = %(dev)s AND a.question_id = q.id
WHERE s.id = %(session_id)s;

-- name: mssv_owner             -> which device already holds this MSSV in this session (None = nobody)
SELECT device_no
FROM session_devices
WHERE session_id = %(session_id)s AND mssv = %(mssv)s;

-- name: roster_check           -> has_roster = session is linked to a class; in_class = MSSV is on its list
SELECT s.class_id IS NOT NULL AS has_roster,
       EXISTS (SELECT 1 FROM class_students cs
               WHERE cs.class_id = s.class_id AND cs.mssv = %(mssv)s) AS in_class,
       (SELECT cs.full_name FROM class_students cs
        WHERE cs.class_id = s.class_id AND cs.mssv = %(mssv)s) AS full_name
FROM sessions s
WHERE s.id = %(session_id)s;

-- name: register_device        -> re-registering the same device replaces MSSV and resets last_seq
--                                 UniqueViolation on (session_id, mssv) = student already on another device
INSERT INTO session_devices (session_id, device_no, mssv, last_seq)
VALUES (%(session_id)s, %(dev)s, %(mssv)s, %(seq)s)
ON CONFLICT (session_id, device_no) DO UPDATE
SET mssv = EXCLUDED.mssv,
    last_seq = EXCLUDED.last_seq,
    registered_at = now()
RETURNING device_no, mssv;

-- name: save_answer            -> "first confirmed answer is locked": never overwritten.
--                                 No row returned = another message locked this question first (race) -> LOCKED
INSERT INTO answers (session_id, device_no, question_id, choice, seq)
VALUES (%(session_id)s, %(dev)s, %(question_id)s, %(choice)s, %(seq)s)
ON CONFLICT (session_id, device_no, question_id) DO NOTHING
RETURNING choice;

-- name: bump_last_seq          -> run in the same transaction as save_answer
UPDATE session_devices
SET last_seq = GREATEST(last_seq, %(seq)s)
WHERE session_id = %(session_id)s AND device_no = %(dev)s;

-- name: set_event_status       -> final status of the raw message (OK, DUP, CLOSED, ...)
UPDATE raw_events SET status = %(status)s
WHERE id = %(event_id)s;


-- ---------------------------------------------------------------------
-- RESULTS (REST API)
-- ---------------------------------------------------------------------

-- name: results_ranking
SELECT rank, mssv, device_no, score, answered
FROM v_ranking
WHERE session_id = %(session_id)s
ORDER BY rank, mssv;

-- name: results_questions
SELECT position, text, correct_choice, registered, answered, correct,
       pct_of_answered, pct_of_registered
FROM v_question_stats
WHERE session_id = %(session_id)s
ORDER BY position;

-- name: score_distribution     -> phổ điểm
SELECT score, score_10, students, n_questions
FROM v_score_distribution
WHERE session_id = %(session_id)s
ORDER BY score;

-- name: event_stats            -> data-quality check for the report
SELECT status, n
FROM v_session_event_stats
WHERE session_id = %(session_id)s
ORDER BY status;


-- ---------------------------------------------------------------------
-- CLASSES AND STUDENT LISTS
-- ---------------------------------------------------------------------

-- name: upsert_class           -> create the class, or update its name/source if the code exists
INSERT INTO classes (code, name, source)
VALUES (%(code)s, %(name)s, %(source)s)
ON CONFLICT (code) DO UPDATE
SET name = COALESCE(EXCLUDED.name, classes.name),
    source = EXCLUDED.source,
    updated_at = now()
RETURNING id, code, name, source, updated_at;

-- name: clear_class_students   -> "replace" mode of an upload
DELETE FROM class_students WHERE class_id = %(class_id)s;

-- name: add_class_student      -> duplicates in one upload are merged (last name wins)
INSERT INTO class_students (class_id, mssv, full_name)
VALUES (%(class_id)s, %(mssv)s, %(full_name)s)
ON CONFLICT (class_id, mssv) DO UPDATE SET full_name = EXCLUDED.full_name;

-- name: list_classes
SELECT c.id, c.code, c.name, c.source, c.updated_at,
       (SELECT COUNT(*) FROM class_students cs WHERE cs.class_id = c.id) AS students
FROM classes c
ORDER BY c.code;

-- name: get_class
SELECT id, code, name, source, updated_at
FROM classes
WHERE id = %(class_id)s;

-- name: get_class_students
SELECT mssv, full_name
FROM class_students
WHERE class_id = %(class_id)s
ORDER BY mssv;

-- name: open_questions         -> questions still open (used at startup: timers live in memory and are lost on restart)
SELECT id, room, current_q, EXTRACT(EPOCH FROM (q_deadline - now()))::float AS delay_s
FROM sessions
WHERE phase = 'QUESTION_OPEN';

-- name: locked_choice          -> the locked choice of a device for question position q (for the ack)
SELECT a.choice
FROM answers a
JOIN sessions s  ON s.id = a.session_id
JOIN questions q ON q.id = a.question_id AND q.quiz_id = s.quiz_id
WHERE a.session_id = %(session_id)s AND a.device_no = %(dev)s AND q.position = %(q)s;


-- ---------------------------------------------------------------------
-- TEACHER LOGIN (app/auth.py)
-- ---------------------------------------------------------------------

-- name: teacher_by_login       -> locked = too many wrong passwords recently
SELECT id, login_id, name, password_hash,
       (locked_until IS NOT NULL AND locked_until > now()) AS locked
FROM teachers
WHERE login_id = %(login_id)s;

-- name: login_failed           -> one more failure; the MAX_FAILS-th locks the account for a while
UPDATE teachers
SET failed_attempts = failed_attempts + 1,
    locked_until = CASE WHEN failed_attempts + 1 >= %(max_fails)s
                        THEN now() + make_interval(mins => %(lock_minutes)s) END
WHERE id = %(id)s;

-- name: login_ok
UPDATE teachers SET failed_attempts = 0, locked_until = NULL WHERE id = %(id)s;

-- name: set_password
UPDATE teachers SET password_hash = %(hash)s, failed_attempts = 0, locked_until = NULL WHERE id = %(id)s;


-- ---------------------------------------------------------------------
-- REMOVING DEVICES (registration phase only)
-- ---------------------------------------------------------------------

-- name: remove_device          -> 0 rows = that device is not registered
DELETE FROM session_devices
WHERE session_id = %(session_id)s AND device_no = %(dev)s
RETURNING device_no, mssv;

-- name: clear_devices
DELETE FROM session_devices
WHERE session_id = %(session_id)s
RETURNING device_no, mssv;


-- ---------------------------------------------------------------------
-- TEACHER CORRECTIONS (complaints / manual entry)
-- ---------------------------------------------------------------------

-- name: override_context       -> the device row is LOCKED (FOR UPDATE) until the transaction ends,
--                                 so two copies of the same request cannot both insert
SELECT sd.mssv, q.id AS question_id, q.n_choices, s.phase, s.current_q
FROM session_devices sd
JOIN sessions s       ON s.id = sd.session_id
LEFT JOIN questions q ON q.quiz_id = s.quiz_id AND q.position = %(q)s
WHERE sd.session_id = %(session_id)s AND sd.device_no = %(dev)s
FOR UPDATE OF sd;

-- name: latest_override        -> the fix that currently counts (None = no fix yet)
SELECT id, choice, reason, note, manual, by_teacher
FROM answer_overrides
WHERE session_id = %(session_id)s AND device_no = %(dev)s AND question_id = %(question_id)s
ORDER BY id DESC
LIMIT 1;

-- name: add_override
INSERT INTO answer_overrides (session_id, device_no, question_id, choice, mssv, reason, note, manual, by_teacher)
VALUES (%(session_id)s, %(dev)s, %(question_id)s, %(choice)s, %(mssv)s, %(reason)s, %(note)s, %(manual)s, %(by)s)
RETURNING id, created_at;

-- name: effective_answers      -> the grid AFTER fixes; one row per device per question that has
--                                 an answer or a fix (choice NULL = cleared by the teacher)
SELECT e.device_no, sd.mssv, q.position AS q, e.choice, (e.choice = q.correct_choice) AS correct,
       e.device_choice, e.seq, e.answered_at,
       e.override_id, e.reason, e.note, e.manual, e.by_teacher, e.fixed_at
FROM v_effective_answers e
JOIN session_devices sd ON sd.session_id = e.session_id AND sd.device_no = e.device_no
JOIN questions q        ON q.id = e.question_id
WHERE e.session_id = %(session_id)s
ORDER BY e.device_no, q.position;

-- name: list_overrides         -> full history of fixes (for the report)
SELECT o.id, o.device_no, o.mssv, q.position AS q, o.choice, o.reason, o.note, o.manual, o.by_teacher, o.created_at
FROM answer_overrides o
JOIN questions q ON q.id = o.question_id
WHERE o.session_id = %(session_id)s
ORDER BY o.id;


-- ---------------------------------------------------------------------
-- SAVED FINAL RESULTS
-- ---------------------------------------------------------------------

-- name: save_results           -> upsert: saving again (retry, later complaint) replaces the payload
INSERT INTO session_results (session_id, teacher, payload, analysis)
VALUES (%(session_id)s, %(teacher)s, %(payload)s, %(analysis)s)
ON CONFLICT (session_id) DO UPDATE
SET teacher    = EXCLUDED.teacher,
    payload    = EXCLUDED.payload,
    analysis   = COALESCE(EXCLUDED.analysis, session_results.analysis),
    version    = session_results.version + 1,
    updated_at = now()
RETURNING session_id, version, saved_at, updated_at;

-- name: get_saved_results
SELECT session_id, teacher, payload, analysis, version, saved_at, updated_at
FROM session_results
WHERE session_id = %(session_id)s;
