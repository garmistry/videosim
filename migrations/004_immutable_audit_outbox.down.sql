DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'videosim_app') THEN
        REVOKE ALL PRIVILEGES ON TABLE feeds, leases, current_alarm_pending,
            alarm_events, workers, worker_reports, current_check_state,
            current_alarms, worker_projection_state, outbox, check_results,
            audit_events, consumer_inbox FROM videosim_app;
        REVOKE ALL PRIVILEGES ON SEQUENCE outbox_id_seq FROM videosim_app;
        REVOKE USAGE ON SCHEMA public FROM videosim_app;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'videosim_publisher') THEN
        REVOKE ALL PRIVILEGES ON TABLE outbox FROM videosim_publisher;
        REVOKE USAGE ON SCHEMA public FROM videosim_publisher;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'videosim_pruner') THEN
        REVOKE ALL PRIVILEGES ON TABLE alarm_events FROM videosim_pruner;
        REVOKE USAGE ON SCHEMA public FROM videosim_pruner;
    END IF;
END;
$$;

DROP TRIGGER IF EXISTS outbox_no_truncate ON outbox;
DROP TRIGGER IF EXISTS outbox_immutable_content ON outbox;
DROP TRIGGER IF EXISTS audit_events_no_truncate ON audit_events;
DROP TRIGGER IF EXISTS audit_events_append_only ON audit_events;
DROP FUNCTION IF EXISTS videosim_grant_runtime_roles();
DROP FUNCTION IF EXISTS videosim_prune_expired_alarm_events(text, integer, integer);
DROP FUNCTION IF EXISTS videosim_reject_outbox_content_mutation();
DROP FUNCTION IF EXISTS videosim_reject_audit_mutation();
DELETE FROM schema_migrations WHERE version = 4;
