-- Round 1c: answers are locked on the first confirm; new ack/raw status LOCKED.
-- Run ONCE on an existing database (keeps all data):
--   docker compose exec db psql -U ars -d ars -f /sql/migrate_lock.sql
-- (A database created from the new schema.sql already has this.)
ALTER TABLE raw_events DROP CONSTRAINT IF EXISTS raw_events_status_check;
ALTER TABLE raw_events ADD CONSTRAINT raw_events_status_check
    CHECK (status IN ('RECEIVED', 'OK', 'DUP', 'CLOSED', 'UNREGISTERED', 'BAD', 'NO_SESSION', 'NOT_IN_CLASS', 'LOCKED'));
