-- =====================================================================
-- ARS - seed data for development
-- Load after schema.sql:
--   docker compose exec db psql -U ars -d ars -f /sql/seed.sql
-- =====================================================================

INSERT INTO teachers (name, email) VALUES
  ('Demo Teacher', 'teacher@example.com');

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
