CREATE TABLE IF NOT EXISTS worker_projection_state (
    tenant_id TEXT NOT NULL,
    stream_id TEXT NOT NULL,
    report_id UUID NOT NULL REFERENCES worker_reports(report_id) ON DELETE RESTRICT,
    worker_id TEXT NOT NULL,
    worker_incarnation_id UUID NOT NULL,
    lease_epoch BIGINT NOT NULL CHECK (lease_epoch > 0),
    config_version BIGINT NOT NULL CHECK (config_version > 0),
    sequence BIGINT NOT NULL CHECK (sequence > 0),
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    state TEXT NOT NULL CHECK (state IN ('pending', 'applied', 'superseded')),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, stream_id),
    FOREIGN KEY (tenant_id, stream_id) REFERENCES feeds(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS worker_projection_pending_idx
    ON worker_projection_state (tenant_id, state, updated_at, stream_id);
