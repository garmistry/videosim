-- Preserve a per-feed generation across physical deletes so an old process
-- cannot pass a config-version ABA after another process deletes and recreates
-- the same feed ID.
CREATE TABLE IF NOT EXISTS feed_generations (
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    id TEXT NOT NULL CHECK (id <> ''),
    config_version BIGINT NOT NULL CHECK (config_version > 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, id)
);

-- Existing feed rows predate this table. Their current version is the starting
-- generation; future store mutations advance this row atomically with feeds.
INSERT INTO feed_generations (tenant_id, id, config_version)
SELECT tenant_id, id, config_version
FROM feeds
ON CONFLICT (tenant_id, id) DO UPDATE
SET config_version = GREATEST(
        feed_generations.config_version,
        EXCLUDED.config_version
    ),
    updated_at = clock_timestamp();

-- Migration 004 owns the least-privilege role policy. Extend its idempotent
-- grant function rather than relying on a one-time direct grant: prepare
-- intentionally revokes all runtime table privileges before every migration.
CREATE OR REPLACE FUNCTION videosim_grant_runtime_roles()
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    REVOKE CREATE ON SCHEMA public FROM PUBLIC;
    REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM PUBLIC;
    REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC;
    REVOKE ALL ON FUNCTION videosim_prune_expired_alarm_events(text, integer, integer)
        FROM PUBLIC;
    REVOKE ALL ON FUNCTION videosim_grant_runtime_roles() FROM PUBLIC;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'videosim_app') THEN
        GRANT USAGE ON SCHEMA public TO videosim_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE feeds, leases,
            current_alarm_pending, alarm_events TO videosim_app;
        GRANT SELECT, INSERT, UPDATE ON TABLE workers, worker_reports,
            current_check_state, current_alarms, worker_projection_state,
            feed_generations TO videosim_app;
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
