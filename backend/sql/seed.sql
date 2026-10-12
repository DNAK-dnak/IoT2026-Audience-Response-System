-- =====================================================================
-- ARS - seed data for development
-- Load after schema.sql:
--   docker compose exec db psql -U ars -d ars -f /sql/seed.sql
-- =====================================================================

-- Demo logins of the teacher page (CHANGE these passwords on the page after the first login):
--   GV001 / 123456     GV002 / abc123
-- Only scrypt hashes are stored; regenerate one with:
--   python -c "from app.auth import hash_password; print(hash_password('new password'))"
INSERT INTO teachers (name, email, login_id, password_hash) VALUES
  ('Giáo viên 001', 'gv001@example.com', 'GV001',
   'scrypt$16384$8$1$99a2f640600dba8931fb06dcbece3786$d5752eab2db3568da81e4a42259d8b2418d840198be511b049046077fef9dcf6'),
  ('Giáo viên 002', 'gv002@example.com', 'GV002',
   'scrypt$16384$8$1$6e94d23b0c802870c64602418316616d$3d15960259f2083133927a3373109d8c976e79cae575e78d16d5287fa8d83af2');

INSERT INTO courses (teacher_id, code, name) VALUES
  (1, 'IOT2026', 'Internet of Things');

INSERT INTO quizzes (course_id, title) VALUES
  (1, 'Week 3 - MQTT basics');

INSERT INTO questions (quiz_id, position, text, n_choices, correct_choice, time_limit_s, review_delay_s, options) VALUES
  (1, 1, 'Which QoS level guarantees at-least-once delivery?',          4, 2, 20, 8,
      '["QoS 0", "QoS 1", "QoS 2", "None"]'),
  (1, 2, 'Which MQTT feature lets a new subscriber get the last value?', 4, 3, 20, 8,
      '["Last Will", "Keepalive", "Retained message", "Clean session"]'),
  (1, 3, 'Which component routes MQTT messages?',                       4, 1, 20, 8,
      '["Broker", "Client", "Topic", "Payload"]');
  -- Q1 options: 1=QoS 0, 2=QoS 1, 3=QoS 2, 4=none
  -- Q2 options: 1=Last Will, 2=Keepalive, 3=Retained message, 4=Clean session
  -- Q3 options: 1=Broker, 2=Client, 3=Topic, 4=Payload

-- Devices 1..60 (the class size target). generate_series makes the rows for us.
INSERT INTO devices (device_no, hw, label)
SELECT n, 'pico2w-ble', 'Clicker ' || lpad(n::text, 2, '0')
FROM generate_series(1, 60) AS n;

-- A demo class with 60 students (22134001 .. 22134060).
INSERT INTO classes (code, name) VALUES ('DH22CNTT01', 'Demo class');
INSERT INTO class_students (class_id, mssv, full_name)
SELECT 1, '221340' || lpad(n::text, 2, '0'), 'Student ' || n
FROM generate_series(1, 60) AS n;
