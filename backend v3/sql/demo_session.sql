-- =====================================================================
-- ARS - demo session: runs a whole quiz with SQL only, so you can check
-- your own schema and views against known results.
-- Load after schema.sql + seed.sql:
--   docker compose exec db psql -U ars -d ars -f /sql/demo_session.sql
-- Expected results are written in the comments.
-- =====================================================================
\set ON_ERROR_STOP on
\echo '== 1. create session (IDLE) =='
INSERT INTO sessions (quiz_id, room) VALUES (1, 'room1') RETURNING id, phase;
-- expected: id 1, IDLE

\echo '== 2. a second active session in room1 must be refused =='
\set ON_ERROR_STOP off
INSERT INTO sessions (quiz_id, room) VALUES (1, 'room1');
-- expected: ERROR duplicate key ... "one_active_session_per_room"
\set ON_ERROR_STOP on

\echo '== 3. start: IDLE -> REGISTER =='
UPDATE sessions SET phase = 'REGISTER'
WHERE id = 1 AND phase = 'IDLE'
RETURNING id, phase;

\echo '== 4. four devices register =='
INSERT INTO session_devices (session_id, device_no, mssv, last_seq) VALUES
  (1, 1, '22134001', 1),
  (1, 2, '22134002', 1),
  (1, 3, '22134003', 1),
  (1, 4, '22134004', 1);

\echo '== 5. same student on a second device must be refused =='
\set ON_ERROR_STOP off
INSERT INTO session_devices (session_id, device_no, mssv, last_seq) VALUES (1, 5, '22134001', 1);
-- expected: ERROR duplicate key ... (session_id, mssv)
\echo '== 6. answer from an unregistered device must be refused by the composite FK =='
INSERT INTO answers (session_id, device_no, question_id, choice, seq) VALUES (1, 9, 1, 2, 2);
-- expected: ERROR ... violates foreign key constraint "answers_session_id_device_no_fkey"
\set ON_ERROR_STOP on

\echo '== 7. next: REGISTER -> QUESTION_OPEN (question 1), deadline from time_limit_s =='
UPDATE sessions s
SET phase       = 'QUESTION_OPEN',
    current_q   = COALESCE(s.current_q, 0) + 1,
    q_opened_at = now(),
    q_deadline  = now() + make_interval(secs => q.time_limit_s)
FROM questions q
WHERE s.id = 1
  AND s.phase IN ('REGISTER', 'QUESTION_CLOSED')
  AND q.quiz_id  = s.quiz_id
  AND q.position = COALESCE(s.current_q, 0) + 1
RETURNING s.id, s.phase, s.current_q, (s.q_deadline - s.q_opened_at) AS window;
-- expected: QUESTION_OPEN, current_q 1, window 00:00:20

\echo '== 8. answers to question 1 (question_id 1, correct = 2) =='
-- the answer upsert: keep only the highest seq per device per question
INSERT INTO answers (session_id, device_no, question_id, choice, seq)
VALUES (1, 1, 1, 2, 2), (1, 2, 1, 1, 2), (1, 3, 1, 3, 2)
ON CONFLICT (session_id, device_no, question_id) DO UPDATE
SET choice = EXCLUDED.choice, seq = EXCLUDED.seq, answered_at = now()
WHERE answers.seq < EXCLUDED.seq;

\echo '== 9. device 2 changes its mind (seq 3, choice 2) -> updated =='
INSERT INTO answers (session_id, device_no, question_id, choice, seq) VALUES (1, 2, 1, 2, 3)
ON CONFLICT (session_id, device_no, question_id) DO UPDATE
SET choice = EXCLUDED.choice, seq = EXCLUDED.seq, answered_at = now()
WHERE answers.seq < EXCLUDED.seq;
-- expected: INSERT 0 1

\echo '== 10. an OLD message from device 2 arrives late (seq 2, choice 1) -> ignored =='
INSERT INTO answers (session_id, device_no, question_id, choice, seq) VALUES (1, 2, 1, 1, 2)
ON CONFLICT (session_id, device_no, question_id) DO UPDATE
SET choice = EXCLUDED.choice, seq = EXCLUDED.seq, answered_at = now()
WHERE answers.seq < EXCLUDED.seq;
-- expected: INSERT 0 0

SELECT device_no, choice, seq FROM answers WHERE session_id = 1 AND question_id = 1 ORDER BY device_no;
-- expected: 1|2|2   2|2|3   3|3|2

\echo '== 11. close question 1 (guard: only if question 1 is still the open one) =='
UPDATE sessions SET phase = 'QUESTION_CLOSED'
WHERE id = 1 AND phase = 'QUESTION_OPEN' AND current_q = 1
RETURNING id, phase, current_q;

\echo '== 12. question 2 (correct = 3): devices 1 and 2 answer, device 3 does not =='
UPDATE sessions s
SET phase = 'QUESTION_OPEN', current_q = COALESCE(s.current_q, 0) + 1,
    q_opened_at = now(), q_deadline = now() + make_interval(secs => q.time_limit_s)
FROM questions q
WHERE s.id = 1 AND s.phase IN ('REGISTER', 'QUESTION_CLOSED')
  AND q.quiz_id = s.quiz_id AND q.position = COALESCE(s.current_q, 0) + 1
RETURNING s.current_q;
INSERT INTO answers (session_id, device_no, question_id, choice, seq)
VALUES (1, 1, 2, 3, 3), (1, 2, 2, 3, 4)
ON CONFLICT (session_id, device_no, question_id) DO UPDATE
SET choice = EXCLUDED.choice, seq = EXCLUDED.seq, answered_at = now()
WHERE answers.seq < EXCLUDED.seq;
UPDATE sessions SET phase = 'QUESTION_CLOSED' WHERE id = 1 AND phase = 'QUESTION_OPEN' AND current_q = 2;

\echo '== 13. question 3 (correct = 1) =='
UPDATE sessions s
SET phase = 'QUESTION_OPEN', current_q = COALESCE(s.current_q, 0) + 1,
    q_opened_at = now(), q_deadline = now() + make_interval(secs => q.time_limit_s)
FROM questions q
WHERE s.id = 1 AND s.phase IN ('REGISTER', 'QUESTION_CLOSED')
  AND q.quiz_id = s.quiz_id AND q.position = COALESCE(s.current_q, 0) + 1
RETURNING s.current_q;
INSERT INTO answers (session_id, device_no, question_id, choice, seq)
VALUES (1, 1, 3, 1, 4), (1, 2, 3, 4, 5), (1, 3, 3, 1, 3)
ON CONFLICT (session_id, device_no, question_id) DO UPDATE
SET choice = EXCLUDED.choice, seq = EXCLUDED.seq, answered_at = now()
WHERE answers.seq < EXCLUDED.seq;

\echo '== 14. deadline check in SQL: pretend a message arrived 1 second after the deadline =='
SELECT (q_deadline + interval '1 second') > q_deadline AS late
FROM sessions WHERE id = 1;
-- expected: t  (the backend compares raw_events.received_at with sessions.q_deadline)

\echo '== 15. there is no question 4: next must change nothing =='
UPDATE sessions SET phase = 'QUESTION_CLOSED' WHERE id = 1 AND phase = 'QUESTION_OPEN' AND current_q = 3;
UPDATE sessions s
SET phase = 'QUESTION_OPEN', current_q = COALESCE(s.current_q, 0) + 1,
    q_opened_at = now(), q_deadline = now() + make_interval(secs => q.time_limit_s)
FROM questions q
WHERE s.id = 1 AND s.phase IN ('REGISTER', 'QUESTION_CLOSED')
  AND q.quiz_id = s.quiz_id AND q.position = COALESCE(s.current_q, 0) + 1
RETURNING s.current_q;
-- expected: UPDATE 0  (the API answers 409: no more questions)

\echo '== 16. end the session =='
UPDATE sessions SET phase = 'ENDED', ended_at = now()
WHERE id = 1 AND phase IN ('REGISTER', 'QUESTION_CLOSED')
RETURNING id, phase;

\echo '== 17. raw layer: some messages with their final status =='
INSERT INTO raw_events (topic, device_no, kind, payload_text, payload, session_id, status) VALUES
  ('ars/room1/dev/01/answer', 1, 'answer', '{"dev":1,"q":1,"choice":2,"seq":2}', '{"dev":1,"q":1,"choice":2,"seq":2}', 1, 'OK'),
  ('ars/room1/dev/02/answer', 2, 'answer', '{"dev":2,"q":1,"choice":1,"seq":2}', '{"dev":2,"q":1,"choice":1,"seq":2}', 1, 'DUP'),
  ('ars/room1/dev/03/answer', 3, 'answer', 'choice=2',                            NULL,                                  1, 'BAD'),
  ('ars/room1/dev/09/answer', 9, 'answer', '{"dev":9,"q":1,"choice":2,"seq":1}', '{"dev":9,"q":1,"choice":2,"seq":1}', 1, 'UNREGISTERED'),
  ('ars/room1/dev/01/answer', 1, 'answer', '{"dev":1,"q":1,"choice":2,"seq":9}', '{"dev":1,"q":1,"choice":2,"seq":9}', 1, 'CLOSED');

\echo '== RESULTS =='
\echo '-- ranking (expected: 22134001 3 | 22134002 2 | 22134003 1 | 22134004 0)'
SELECT rank, mssv, device_no, score, answered FROM v_ranking WHERE session_id = 1 ORDER BY rank, mssv;

\echo '-- question stats (expected pct_of_answered 66.7 / 100.0 / 66.7, pct_of_registered 50.0 each)'
SELECT position, correct_choice, registered, answered, correct, pct_of_answered, pct_of_registered
FROM v_question_stats WHERE session_id = 1 ORDER BY position;

\echo '-- raw event statuses'
SELECT status, n FROM v_session_event_stats WHERE session_id = 1 ORDER BY status;

\echo '-- a new session in room1 is allowed now that session 1 has ENDED'
INSERT INTO sessions (quiz_id, room) VALUES (1, 'room1') RETURNING id, phase;
-- expected: IDLE, with id 3 (not 2): the refused insert in step 2 used up id 2.
-- Sequences never roll back, so gaps in ids are normal.
