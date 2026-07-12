-- Enforce append-only durable audit evidence and immutable outbox identity/content.
-- Delivery-state columns on outbox remain mutable for the at-least-once publisher.

CREATE OR REPLACE FUNCTION videosim_reject_audit_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only';
END;
$$;

CREATE OR REPLACE FUNCTION videosim_reject_outbox_content_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP IN ('DELETE', 'TRUNCATE') THEN
        RAISE EXCEPTION 'outbox events cannot be deleted or truncated';
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.event_id IS DISTINCT FROM OLD.event_id
       OR NEW.subject IS DISTINCT FROM OLD.subject
       OR NEW.payload IS DISTINCT FROM OLD.payload
       OR NEW.payload_sha256 IS DISTINCT FROM OLD.payload_sha256
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'outbox event identity and content are immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS audit_events_append_only ON audit_events;
CREATE TRIGGER audit_events_append_only
BEFORE UPDATE OR DELETE ON audit_events
FOR EACH ROW EXECUTE FUNCTION videosim_reject_audit_mutation();

DROP TRIGGER IF EXISTS audit_events_no_truncate ON audit_events;
CREATE TRIGGER audit_events_no_truncate
BEFORE TRUNCATE ON audit_events
FOR EACH STATEMENT EXECUTE FUNCTION videosim_reject_audit_mutation();

DROP TRIGGER IF EXISTS outbox_immutable_content ON outbox;
CREATE TRIGGER outbox_immutable_content
BEFORE UPDATE OR DELETE ON outbox
FOR EACH ROW EXECUTE FUNCTION videosim_reject_outbox_content_mutation();

DROP TRIGGER IF EXISTS outbox_no_truncate ON outbox;
CREATE TRIGGER outbox_no_truncate
BEFORE TRUNCATE ON outbox
FOR EACH STATEMENT EXECUTE FUNCTION videosim_reject_outbox_content_mutation();

-- The pruner needs exclusive candidate locks to avoid duplicate work, which
-- PostgreSQL treats as UPDATE authority. Keep that authority inside a tightly
-- scoped security-definer function rather than granting the service role UPDATE
-- on alarm-event evidence.
CREATE OR REPLACE FUNCTION videosim_prune_expired_alarm_events(
    target_tenant_id text,
    retention_seconds integer,
    requested_batch_size integer
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    deleted_count integer;
BEGIN
    IF requested_batch_size < 1 OR requested_batch_size > 10000 THEN
        RAISE EXCEPTION 'alarm-event prune batch_size must be between 1 and 10000';
    END IF;
    IF retention_seconds < 1 THEN
        RAISE EXCEPTION 'alarm-event retention_seconds must be positive';
    END IF;

    WITH expired AS (
        SELECT event_id
        FROM public.alarm_events
        WHERE tenant_id = target_tenant_id
          AND created_at < clock_timestamp()
              - (retention_seconds * interval '1 second')
        ORDER BY created_at, event_id
        LIMIT requested_batch_size
        FOR UPDATE SKIP LOCKED
    ), deleted AS (
        DELETE FROM public.alarm_events
        WHERE event_id IN (SELECT event_id FROM expired)
        RETURNING event_id
    )
    SELECT count(*)::integer INTO deleted_count FROM deleted;
    RETURN deleted_count;
END;
$$;
REVOKE ALL ON FUNCTION videosim_prune_expired_alarm_events(text, integer, integer)
    FROM PUBLIC;

-- Production Compose runs each service as a non-owner role provisioned by
-- postgres-runtime-role.sh before migrations. The same owner-only function is
-- called again after every migration run, so a restart converges a formerly
-- overprivileged role without leaving it unable to run an already-applied
-- migration. Independent reporting roles must be granted explicitly rather
-- than relying on PUBLIC access to control-plane tables.
CREATE OR REPLACE FUNCTION videosim_grant_runtime_roles()
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    REVOKE CREATE ON SCHEMA public FROM PUBLIC;
    REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM PUBLIC;
    REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC;
    -- pg_restore --no-privileges recreates function defaults, so revoke public
    -- execution here as well as at migration creation time.
    REVOKE ALL ON FUNCTION videosim_prune_expired_alarm_events(text, integer, integer)
        FROM PUBLIC;
    REVOKE ALL ON FUNCTION videosim_grant_runtime_roles() FROM PUBLIC;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'videosim_app') THEN
        GRANT USAGE ON SCHEMA public TO videosim_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE feeds, leases,
            current_alarm_pending, alarm_events TO videosim_app;
        GRANT SELECT, INSERT, UPDATE ON TABLE workers, worker_reports,
            current_check_state, current_alarms, worker_projection_state
            TO videosim_app;
        GRANT SELECT, INSERT ON TABLE check_results, audit_events, outbox,
            consumer_inbox TO videosim_app;
        GRANT USAGE, SELECT ON SEQUENCE outbox_id_seq TO videosim_app;
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'videosim_publisher') THEN
        GRANT USAGE ON SCHEMA public TO videosim_publisher;
        GRANT SELECT, UPDATE ON TABLE outbox TO videosim_publisher;
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'videosim_pruner') THEN
        GRANT USAGE ON SCHEMA public TO videosim_pruner;
        GRANT EXECUTE ON FUNCTION
            videosim_prune_expired_alarm_events(text, integer, integer)
            TO videosim_pruner;
    END IF;
END;
$$;
REVOKE ALL ON FUNCTION videosim_grant_runtime_roles() FROM PUBLIC;
SELECT videosim_grant_runtime_roles();
