-- =====================================================================
-- ARS (Audience Response System) - database schema
-- PostgreSQL 16
-- Load:  docker compose exec db psql -U ars -d ars -f /sql/schema.sql
-- Reloading this file DELETES all data (development only).
-- =====================================================================

-- Drop in reverse dependency order ------------------------------------
DROP VIEW  IF EXISTS v_ranking, v_question_stats, v_session_event_stats, v_score_distribution CASCADE;
DROP TABLE IF EXISTS answers, raw_events, session_devices, sessions,
                     devices, questions, quizzes, courses, teachers,
                     class_students, classes CASCADE;

-- =====================================================================
-- SETUP DATA (dimension tables): who teaches what, and the questions
-- =====================================================================

CREATE TABLE teachers (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT        NOT NULL,
    email       TEXT        NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE courses (
    id          BIGSERIAL PRIMARY KEY,
    teacher_id  BIGINT NOT NULL REFERENCES teachers(id),
    code        TEXT   NOT NULL,                 -- e.g. 'IOT2026'
    name        TEXT   NOT NULL,
    UNIQUE (teacher_id, code)                    -- a teacher cannot have two courses with the same code
);

CREATE TABLE quizzes (
    id          BIGSERIAL PRIMARY KEY,
    course_id   BIGINT      NOT NULL REFERENCES courses(id),
    title       TEXT        NOT NULL,
    subject     TEXT,                              -- free text from the page ("Môn học")
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A class (lớp) and its student list. Today the teacher uploads it (Excel/CSV parsed by the
-- page); `source` leaves room for an automatic sync from a school system later.
CREATE TABLE classes (
    id          BIGSERIAL PRIMARY KEY,
    code        TEXT        NOT NULL UNIQUE,       -- e.g. 'DH22CNTT01'
    name        TEXT,
    source      TEXT        NOT NULL DEFAULT 'upload' CHECK (source IN ('upload', 'school_sync')),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE class_students (
    class_id    BIGINT NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    mssv        TEXT   NOT NULL CHECK (mssv ~ '^[0-9]{8}$'),
    full_name   TEXT,
    PRIMARY KEY (class_id, mssv)
);

CREATE TABLE questions (
    id              BIGSERIAL PRIMARY KEY,
    quiz_id         BIGINT  NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
    position        INTEGER NOT NULL CHECK (position >= 1),       -- 1, 2, 3 ... order inside the quiz
    text            TEXT    NOT NULL,
    n_choices       INTEGER NOT NULL CHECK (n_choices BETWEEN 2 AND 5),
    correct_choice  INTEGER NOT NULL,
    time_limit_s    INTEGER NOT NULL DEFAULT 20 CHECK (time_limit_s BETWEEN 5 AND 600),
    review_delay_s  INTEGER NOT NULL DEFAULT 8  CHECK (review_delay_s BETWEEN 0 AND 120),  -- pause after the question (page auto-advance)
    options         JSONB   NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(options) = 'array'),  -- ["O(1)","O(log n)",...]
    UNIQUE (quiz_id, position),
    CHECK (correct_choice BETWEEN 1 AND n_choices)                -- compares two columns of the same row
);

-- Physical clickers. device_no is printed on the device (1 to n), so we choose it: no BIGSERIAL.
CREATE TABLE devices (
    device_no   INTEGER PRIMARY KEY CHECK (device_no >= 1),
    hw          TEXT    NOT NULL DEFAULT 'pico2w-ble'
                CHECK (hw IN ('pico2w-ble', 'pico2w', 'phone', 'sim')),
    label       TEXT                                              -- optional note, e.g. 'red case'
);

-- =====================================================================
-- RUNS OF A QUIZ
-- =====================================================================

-- One session = one run of a quiz in one room. current_q is the question POSITION currently open.
CREATE TABLE sessions (
    id           BIGSERIAL PRIMARY KEY,
    quiz_id      BIGINT      NOT NULL REFERENCES quizzes(id),
    class_id     BIGINT      REFERENCES classes(id),   -- NULL = no student-list check
    room         TEXT        NOT NULL,
    phase        TEXT        NOT NULL DEFAULT 'IDLE'
                 CHECK (phase IN ('IDLE', 'REGISTER', 'QUESTION_OPEN', 'QUESTION_CLOSED', 'ENDED')),
    current_q    INTEGER     CHECK (current_q >= 1),              -- NULL until the first question opens
    q_opened_at  TIMESTAMPTZ,
    q_deadline   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at     TIMESTAMPTZ,
    -- when a question is open, we must know which one and until when
    CHECK (phase <> 'QUESTION_OPEN'
           OR (current_q IS NOT NULL AND q_opened_at IS NOT NULL AND q_deadline IS NOT NULL))
);

-- Only one active (not ENDED) session per room: a PARTIAL unique index.
CREATE UNIQUE INDEX one_active_session_per_room
    ON sessions (room) WHERE phase <> 'ENDED';

-- Who holds which device in this session. Link table between sessions and devices.
CREATE TABLE session_devices (
    session_id     BIGINT      NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    device_no      INTEGER     NOT NULL REFERENCES devices(device_no),
    mssv           TEXT        NOT NULL CHECK (mssv ~ '^[0-9]{8}$'),   -- 8-digit student ID, kept as text
    registered_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seq       INTEGER     NOT NULL DEFAULT 0 CHECK (last_seq >= 0),  -- highest seq accepted from this device
    PRIMARY KEY (session_id, device_no),                           -- a device registers once per session
    UNIQUE (session_id, mssv)                                      -- a student uses one device per session
);

-- =====================================================================
-- RAW LAYER: every MQTT message exactly as received (insert-only)
-- =====================================================================

CREATE TABLE raw_events (
    id            BIGSERIAL PRIMARY KEY,
    received_at   TIMESTAMPTZ NOT NULL DEFAULT now(),   -- the ONE clock used for deadlines
    topic         TEXT        NOT NULL,
    device_no     INTEGER,            -- parsed from the topic; NULL if the topic is malformed (no FK on purpose)
    kind          TEXT,               -- 'register' | 'answer' | NULL if unknown
    payload_text  TEXT        NOT NULL,                 -- always kept, even if not JSON
    payload       JSONB,                                -- NULL when payload_text is not valid JSON
    session_id    BIGINT      REFERENCES sessions(id),  -- NULL when no session was active
    status        TEXT        NOT NULL DEFAULT 'RECEIVED'
                  CHECK (status IN ('RECEIVED', 'OK', 'DUP', 'CLOSED', 'UNREGISTERED', 'BAD', 'NO_SESSION', 'NOT_IN_CLASS'))
);

CREATE INDEX raw_events_session_idx  ON raw_events (session_id);
CREATE INDEX raw_events_received_idx ON raw_events (received_at);

-- =====================================================================
-- CLEANED LAYER: one valid answer per device per question per session (the fact table)
-- =====================================================================

CREATE TABLE answers (
    session_id   BIGINT      NOT NULL,
    device_no    INTEGER     NOT NULL,
    question_id  BIGINT      NOT NULL REFERENCES questions(id),
    choice       INTEGER     NOT NULL CHECK (choice >= 1),
    seq          INTEGER     NOT NULL CHECK (seq >= 1),
    answered_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, device_no, question_id),
    -- composite FK: an answer can only come from a device REGISTERED in this session
    FOREIGN KEY (session_id, device_no)
        REFERENCES session_devices (session_id, device_no) ON DELETE CASCADE
);

-- =====================================================================
-- RESULTS LAYER (views: always up to date, nothing stored)
-- =====================================================================

-- % correct per question, per session.
-- pct_of_answered   = correct / students who answered this question
-- pct_of_registered = correct / all registered students (non-answers count as wrong)
CREATE VIEW v_question_stats AS
WITH reg AS (
    SELECT session_id, COUNT(*) AS registered
    FROM session_devices
    GROUP BY session_id
)
SELECT s.id                                             AS session_id,
       q.id                                             AS question_id,
       q.position,
       q.text,
       q.correct_choice,
       COALESCE(reg.registered, 0)                      AS registered,
       COUNT(a.device_no)                               AS answered,
       COUNT(a.device_no) FILTER (WHERE a.choice = q.correct_choice) AS correct,
       ROUND(100.0 * COUNT(a.device_no) FILTER (WHERE a.choice = q.correct_choice)
             / NULLIF(COUNT(a.device_no), 0), 1)        AS pct_of_answered,
       ROUND(100.0 * COUNT(a.device_no) FILTER (WHERE a.choice = q.correct_choice)
             / NULLIF(COALESCE(reg.registered, 0), 0), 1) AS pct_of_registered
FROM sessions s
JOIN questions q     ON q.quiz_id = s.quiz_id
LEFT JOIN answers a  ON a.session_id = s.id AND a.question_id = q.id
LEFT JOIN reg        ON reg.session_id = s.id
GROUP BY s.id, q.id, q.position, q.text, q.correct_choice, reg.registered;

-- Ranking per session: every registered student, score = number of correct answers.
CREATE VIEW v_ranking AS
WITH scores AS (
    SELECT sd.session_id,
           sd.device_no,
           sd.mssv,
           COUNT(a.question_id)                                   AS answered,
           COUNT(a.question_id) FILTER (WHERE a.choice = q.correct_choice) AS score
    FROM session_devices sd
    LEFT JOIN answers a   ON a.session_id = sd.session_id AND a.device_no = sd.device_no
    LEFT JOIN questions q ON q.id = a.question_id
    GROUP BY sd.session_id, sd.device_no, sd.mssv
)
SELECT session_id,
       RANK() OVER (PARTITION BY session_id ORDER BY score DESC) AS rank,
       mssv,
       device_no,
       score,
       answered
FROM scores;

-- Data-quality view: how many raw messages ended in each status, per session.
CREATE VIEW v_session_event_stats AS
SELECT session_id, status, COUNT(*) AS n
FROM raw_events
GROUP BY session_id, status;

-- Score distribution (phổ điểm): how many students got each number of correct answers.
-- Every score from 0 to the number of questions appears, even with 0 students.
CREATE VIEW v_score_distribution AS
WITH nq AS (
    SELECT s.id AS session_id, COUNT(q.id)::int AS n_questions
    FROM sessions s
    JOIN questions q ON q.quiz_id = s.quiz_id
    GROUP BY s.id
)
SELECT nq.session_id,
       g.score::int                                        AS score,
       nq.n_questions,
       ROUND(10.0 * g.score / NULLIF(nq.n_questions, 0), 2) AS score_10,
       COUNT(r.mssv)                                       AS students
FROM nq
CROSS JOIN LATERAL generate_series(0, nq.n_questions) AS g(score)
LEFT JOIN v_ranking r ON r.session_id = nq.session_id AND r.score = g.score
GROUP BY nq.session_id, g.score, nq.n_questions;
