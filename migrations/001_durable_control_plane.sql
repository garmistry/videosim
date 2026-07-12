CREATE TABLE IF NOT EXISTS control_plane_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    generation BIGINT NOT NULL DEFAULT 1 CHECK (generation > 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO control_plane_state (singleton) VALUES (TRUE) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY CHECK (id <> ''),
    name TEXT NOT NULL CHECK (name <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO tenants (id, name) VALUES ('default', 'Default tenant') ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS feeds (
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    id TEXT NOT NULL CHECK (id <> ''),
    config JSONB NOT NULL CHECK (jsonb_typeof(config) = 'object'),
    config_version BIGINT NOT NULL DEFAULT 1 CHECK (config_version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, id)
);
CREATE INDEX IF NOT EXISTS feeds_updated_idx ON feeds (tenant_id, updated_at, id);

CREATE TABLE IF NOT EXISTS workers (
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    worker_id TEXT NOT NULL CHECK (worker_id <> ''),
    incarnation_id UUID NOT NULL,
    certificate_subject TEXT NOT NULL CHECK (certificate_subject <> ''),
    capabilities JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(capabilities) = 'object'),
    capacity JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(capacity) = 'object'),
    software_version TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL CHECK (state IN ('active', 'draining', 'offline', 'revoked')),
    last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, worker_id)
);
CREATE INDEX IF NOT EXISTS workers_heartbeat_idx ON workers (tenant_id, state, last_heartbeat_at);

CREATE TABLE IF NOT EXISTS leases (
    tenant_id TEXT NOT NULL,
    stream_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    worker_incarnation_id UUID NOT NULL,
    epoch BIGINT NOT NULL CHECK (epoch > 0),
    config_version BIGINT NOT NULL CHECK (config_version > 0),
    issued_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    expires_at TIMESTAMPTZ NOT NULL,
    acknowledged_at TIMESTAMPTZ,
    state TEXT NOT NULL CHECK (state IN ('offered', 'active', 'draining', 'expired', 'revoked')),
    last_sequence BIGINT NOT NULL DEFAULT 0 CHECK (last_sequence >= 0),
    PRIMARY KEY (tenant_id, stream_id),
    FOREIGN KEY (tenant_id, stream_id) REFERENCES feeds(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, worker_id) REFERENCES workers(tenant_id, worker_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS leases_worker_idx ON leases (tenant_id, worker_id, expires_at);
CREATE INDEX IF NOT EXISTS leases_expiry_idx ON leases (tenant_id, expires_at);

CREATE TABLE IF NOT EXISTS worker_reports (
    report_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    worker_id TEXT NOT NULL,
    worker_incarnation_id UUID NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    disposition JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(disposition) = 'object'),
    received_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    committed_at TIMESTAMPTZ,
    FOREIGN KEY (tenant_id, worker_id) REFERENCES workers(tenant_id, worker_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS worker_reports_received_idx ON worker_reports (tenant_id, received_at DESC);

CREATE TABLE IF NOT EXISTS check_results (
    result_id UUID PRIMARY KEY,
    report_id UUID NOT NULL REFERENCES worker_reports(report_id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL,
    stream_id TEXT NOT NULL,
    check_id TEXT NOT NULL CHECK (check_id <> ''),
    lease_epoch BIGINT NOT NULL,
    config_version BIGINT NOT NULL,
    sequence BIGINT NOT NULL CHECK (sequence > 0),
    status TEXT NOT NULL CHECK (status IN ('healthy', 'unhealthy', 'unknown', 'stale', 'error', 'timeout', 'skipped')),
    observed_at TIMESTAMPTZ NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) = 'object'),
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, stream_id, check_id, lease_epoch, sequence),
    FOREIGN KEY (tenant_id, stream_id) REFERENCES feeds(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS check_results_stream_time_idx ON check_results (tenant_id, stream_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS current_check_state (
    tenant_id TEXT NOT NULL,
    stream_id TEXT NOT NULL,
    check_id TEXT NOT NULL CHECK (check_id <> ''),
    result_id UUID NOT NULL REFERENCES check_results(result_id) ON DELETE RESTRICT,
    lease_epoch BIGINT NOT NULL,
    config_version BIGINT NOT NULL,
    sequence BIGINT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('healthy', 'unhealthy', 'unknown', 'stale', 'error', 'timeout', 'skipped')),
    observed_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) = 'object'),
    PRIMARY KEY (tenant_id, stream_id, check_id),
    FOREIGN KEY (tenant_id, stream_id) REFERENCES feeds(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS current_alarms (
    tenant_id TEXT NOT NULL,
    stream_id TEXT NOT NULL,
    monitor_id TEXT NOT NULL,
    active BOOLEAN NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    source_result_id UUID REFERENCES check_results(result_id) ON DELETE SET NULL,
    lease_epoch BIGINT NOT NULL,
    config_version BIGINT NOT NULL,
    sequence BIGINT NOT NULL,
    raised_at TIMESTAMPTZ,
    cleared_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, stream_id, monitor_id),
    FOREIGN KEY (tenant_id, stream_id) REFERENCES feeds(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS current_alarms_active_idx ON current_alarms (tenant_id, active, severity, updated_at DESC);

CREATE TABLE IF NOT EXISTS alarm_events (
    event_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    stream_id TEXT NOT NULL,
    monitor_id TEXT NOT NULL,
    transition TEXT NOT NULL CHECK (transition IN ('raised', 'active', 'cleared', 'acknowledged', 'suppressed')),
    source_result_id UUID REFERENCES check_results(result_id) ON DELETE SET NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    occurred_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (tenant_id, stream_id) REFERENCES feeds(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS alarm_events_stream_time_idx ON alarm_events (tenant_id, stream_id, occurred_at DESC, event_id);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    principal_kind TEXT NOT NULL,
    principal_subject TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(payload) = 'object'),
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    occurred_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS audit_events_time_idx ON audit_events (tenant_id, occurred_at DESC, event_id);

CREATE TABLE IF NOT EXISTS outbox (
    id BIGSERIAL PRIMARY KEY,
    event_id UUID NOT NULL UNIQUE,
    subject TEXT NOT NULL CHECK (subject <> ''),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'publishing', 'published', 'dead')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    publisher_id TEXT,
    locked_at TIMESTAMPTZ,
    published_at TIMESTAMPTZ,
    broker_sequence BIGINT,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS outbox_claim_idx ON outbox (state, next_attempt_at, id);

CREATE TABLE IF NOT EXISTS consumer_inbox (
    consumer_name TEXT NOT NULL CHECK (consumer_name <> ''),
    event_id UUID NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    processed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (consumer_name, event_id)
);
