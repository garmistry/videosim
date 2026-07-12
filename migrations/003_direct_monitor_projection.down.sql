DROP INDEX IF EXISTS alarm_events_tenant_created_idx;
DROP INDEX IF EXISTS current_alarm_pending_read_idx;
DROP TABLE IF EXISTS current_alarm_pending;
ALTER TABLE current_alarms DROP COLUMN IF EXISTS last_event_at;
DELETE FROM schema_migrations WHERE version = 3;
