-- =====================================================================
-- Round 4: teacher login, removing devices, complaints (answer_overrides),
--          saved final results, AI analysis.
-- Run ONCE on an existing database (keeps all data):
--   docker compose exec db psql -U ars -d ars -f /sql/migrate_v4.sql
-- A database created from the new schema.sql + seed.sql already has all of this.
-- Safe to run twice.
-- =====================================================================
BEGIN;

ALTER TABLE teachers ADD COLUMN IF NOT EXISTS login_id        TEXT UNIQUE CHECK (login_id ~ '^[A-Z0-9_.-]{2,20}$');
ALTER TABLE teachers ADD COLUMN IF NOT EXISTS password_hash   TEXT;
ALTER TABLE teachers ADD COLUMN IF NOT EXISTS failed_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE teachers ADD COLUMN IF NOT EXISTS locked_until    TIMESTAMPTZ;

-- demo logins GV001 / 123456 (the existing teacher 1) and GV002 / abc123
UPDATE teachers SET login_id = 'GV001', name = 'Giáo viên 001',
       password_hash = 'scrypt$16384$8$1$99a2f640600dba8931fb06dcbece3786$d5752eab2db3568da81e4a42259d8b2418d840198be511b049046077fef9dcf6'
WHERE id = (SELECT MIN(id) FROM teachers) AND login_id IS NULL
  AND NOT EXISTS (SELECT 1 FROM teachers WHERE login_id = 'GV001');
INSERT INTO teachers (name, email, login_id, password_hash)
SELECT 'Giáo viên 002', 'gv002@example.com', 'GV002',
       'scrypt$16384$8$1$6e94d23b0c802870c64602418316616d$3d15960259f2083133927a3373109d8c976e79cae575e78d16d5287fa8d83af2'
WHERE NOT EXISTS (SELECT 1 FROM teachers WHERE login_id = 'GV002' OR email = 'gv002@example.com');

CREATE TABLE IF NOT EXISTS answer_overrides (
    id           BIGSERIAL PRIMARY KEY,
    session_id   BIGINT      NOT NULL,
    device_no    INTEGER     NOT NULL,
    question_id  BIGINT      NOT NULL REFERENCES questions(id),
    choice       INTEGER     CHECK (choice >= 1),
    mssv         TEXT        NOT NULL,             -- who held the device when the fix was made
    reason       TEXT        NOT NULL,
    note         TEXT,
    manual       BOOLEAN     NOT NULL DEFAULT false, -- true = typed in for a device that sent nothing
    by_teacher   TEXT,                              -- login_id of the teacher
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (session_id, device_no)
        REFERENCES session_devices (session_id, device_no) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS answer_overrides_latest_idx ON answer_overrides (session_id, device_no, question_id, id DESC);

CREATE TABLE IF NOT EXISTS session_results (
    session_id   BIGINT      PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    teacher      TEXT,
    payload      JSONB       NOT NULL,
    analysis     TEXT,
    version      INTEGER     NOT NULL DEFAULT 1,     -- how many times it was saved
    saved_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- the result views now count the teacher's fixes
DROP VIEW IF EXISTS v_score_distribution, v_ranking, v_question_stats, v_effective_answers CASCADE;

CREATE VIEW v_effective_answers AS
WITH latest AS (
    SELECT DISTINCT ON (session_id, device_no, question_id) *
    FROM answer_overrides
    ORDER BY session_id, device_no, question_id, id DESC
)
SELECT COALESCE(a.session_id, o.session_id)   AS session_id,
       COALESCE(a.device_no, o.device_no)     AS device_no,
       COALESCE(a.question_id, o.question_id) AS question_id,
       CASE WHEN o.id IS NULL THEN a.choice ELSE o.choice END AS choice,
       a.choice      AS device_choice,          -- what the device sent (NULL = nothing)
       a.seq,
       a.answered_at,
       o.id          AS override_id,
       o.reason, o.note, o.manual, o.by_teacher, o.created_at AS fixed_at
FROM answers a
FULL JOIN latest o
       ON o.session_id = a.session_id AND o.device_no = a.device_no AND o.question_id = a.question_id;

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
       COUNT(a.choice)                                  AS answered,
       COUNT(a.device_no) FILTER (WHERE a.choice = q.correct_choice) AS correct,
       ROUND(100.0 * COUNT(a.device_no) FILTER (WHERE a.choice = q.correct_choice)
             / NULLIF(COUNT(a.choice), 0), 1)        AS pct_of_answered,
       ROUND(100.0 * COUNT(a.device_no) FILTER (WHERE a.choice = q.correct_choice)
             / NULLIF(COALESCE(reg.registered, 0), 0), 1) AS pct_of_registered
FROM sessions s
JOIN questions q     ON q.quiz_id = s.quiz_id
LEFT JOIN v_effective_answers a ON a.session_id = s.id AND a.question_id = q.id
LEFT JOIN reg        ON reg.session_id = s.id
GROUP BY s.id, q.id, q.position, q.text, q.correct_choice, reg.registered;

-- Ranking per session: every registered student, score = number of correct answers.
CREATE VIEW v_ranking AS
WITH scores AS (
    SELECT sd.session_id,
           sd.device_no,
           sd.mssv,
           COUNT(a.choice)                                        AS answered,
           COUNT(a.question_id) FILTER (WHERE a.choice = q.correct_choice) AS score
    FROM session_devices sd
    LEFT JOIN v_effective_answers a ON a.session_id = sd.session_id AND a.device_no = sd.device_no
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

COMMIT;
