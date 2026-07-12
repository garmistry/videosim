-- Direct PostgreSQL monitor projection for the worker-v2 control plane.
-- This is expand-only: the legacy JSON shadow remains readable only by the
-- SQLite/trusted-lab path while PostgreSQL projects catalog monitor evidence
-- atomically with fenced result ingestion.

ALTER TABLE current_alarms
    ADD COLUMN IF NOT EXISTS last_event_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS current_alarm_pending (
    tenant_id TEXT NOT NULL,
    stream_id TEXT NOT NULL,
    monitor_id TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    source_result_id UUID NOT NULL REFERENCES check_results(result_id) ON DELETE CASCADE,
    lease_epoch BIGINT NOT NULL,
    config_version BIGINT NOT NULL,
    sequence BIGINT NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, stream_id, monitor_id),
    FOREIGN KEY (tenant_id, stream_id) REFERENCES feeds(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS current_alarm_pending_read_idx
    ON current_alarm_pending (tenant_id, stream_id, updated_at DESC, monitor_id);

CREATE INDEX IF NOT EXISTS alarm_events_tenant_created_idx
    ON alarm_events (tenant_id, stream_id, created_at DESC, event_id DESC);
