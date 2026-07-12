# Distributed Architecture Refit

> This document describes the implemented first slice and near-term seams. It is
> not a production-scale design or capacity claim. See
> [Production-Readiness and Thousand-Stream Scaling Audit](production-readiness-audit.md)
> for the evidence-backed risk assessment, target architecture, capacity model,
> migration gates, and verification requirements for 1,000+ monitored streams.

## Target Shape

VideoSim should split into a master control plane and many worker nodes:

- Master control plane: GUI/API, feed registration database, stream assignment endpoint, monitor report aggregation, and operator-facing alarm/event views.
- Worker node: polls the master for assigned streams, validates/monitors those feeds, and posts alarm/event state back to the master.
- Feed runtime: generated feeds can still run beside the master for now; external feeds are monitored from worker nodes through their registered endpoints.
- Storage boundary: feed definitions stay behind the `FeedRegistrationStore` interface. SQLite remains the lab default; the production overlay selects the PostgreSQL adapter.

## Data Flow

1. Operators create or update feeds in the GUI.
2. The master persists feed definitions and alert profiles through the feed-store boundary.
3. Workers call the assignment API with a process UUID incarnation.
4. SQLite trusted-lab mode returns worker v1 process-instance/generation/token
   fencing. PostgreSQL mode returns worker v2 deterministic round-robin
   assignments with durable offered/active lease epoch, config version, and
   expiry.
5. V2 workers acknowledge exact offered lease tuples before probing. An
   independent authenticated heartbeat refreshes worker membership and extends
   only active leases for the same incarnation.
6. Workers prune retained state, reuse the existing monitor engine, and post an
   immutable report UUID, per-lease epoch/config sequence, claimed lease tuples,
   bounded probe metrics, and bounded catalog monitor observations.
7. PostgreSQL revalidates worker/incarnation/heartbeat/lease/epoch/config/
   expiry/sequence/observation time, commits accepted `probe.*` and catalog
   monitor results, pending/current alarm state, alarm edges, and outbox events
   atomically, then returns explicit duplicate/rejection disposition.
8. `/state.json` reads the PostgreSQL monitor projection in durable mode.
   Missing/inconclusive observations cannot clear active or pending alarms;
   stale duplicates/reassigned reports cannot rerun projection. SQLite v1 alone
   retains the file-backed JSON monitor view.

## Implemented First Slice

- Master worker APIs:
  - `GET /api/workers/assignments`
  - `POST /api/workers/register`
  - `POST /api/workers/report`
- `python -m videosim worker` CLI loop for polling assignments and reporting monitor state.
- Compose sample worker service:
  - `docker compose up --build app worker`
- Generated DASH assignments include a master HTTP `monitorEndpoint` so workers do not need a shared DASH volume.
- Generated SRT workers use `--srt-host` to reach listener feeds through the master/app container host name.
- Worker contract `videosim.worker/v1` adds process-restart, generation, and assignment-token fencing. HTTP 409 causes a bounded assignment refetch instead of silently applying stale state.
- Reports are server-scoped: caller-supplied stream IDs cannot grant ownership, retained state is pruned on workers, and malformed/out-of-scope state cannot replace another worker's alarms.
- Workers heartbeat independently of serial probe batches; the default interval is 20 seconds versus the current 60-second registry TTL.
- Latest-batch probe metrics classify success, issue, error, timeout, and skipped checks with monotonic durations. Metrics are replaced, assignment-scoped summaries rather than unbounded history.
- `python -m videosim control-plane-benchmark` exercises deterministic in-process assignment/report invariants. Its output explicitly states that it runs no media probes and is not capacity certification.

## Implemented Compose/VM Security Boundary

- `docker-compose.production.yml` keeps the app port internal and exposes
  separate operator TLS and worker-mTLS listeners through Nginx.
- oauth2-proxy performs operator OIDC login; the app enforces viewer/admin group
  authorization from trusted headers.
- Nginx verifies worker certificates and derives worker identity from the
  certificate CN; the app requires an exact `workerId` match.
- A proxy-only shared secret prevents direct spoofing of trusted identity
  headers when the app network is kept internal.
- Nginx and app body/report limits, per-plane rate limits, endpoint address
  policy, structured authorization events, worker TLS client options, and
  bounded transient retries form the current security gate.
- See [Security and Identity](security.md) for setup and remaining boundaries.

## Implemented Durable Foundation

- PostgreSQL migrations and a bounded-pool repository now model feed config
  versions, worker incarnations, fresh offered/acknowledged expiring leases,
  immutable reports/results, bounded observation time, current check/alarm
  projection with inconclusive-state preservation, alarm/audit events, and a
  transactional outbox.
- NATS JetStream is provisioned as at-least-once committed-event transport.
  Outbox publisher claims are concurrent-safe, use hash-immutable event IDs,
  retain acknowledgement sequence, and retry before dead state. A consumer
  inbox schema exists, but no consumer is deployed.
- The production Compose overlay gates app startup on migrations and publisher
  startup on JetStream initialization. Backup/restore, restored-lease expiry
  fencing, explicit broker recovery mode, and SQLite feed import tools exist.
- PostgreSQL-backed HTTP endpoints and `run_worker` now negotiate worker v2,
  including offer/ack/report/retry and incarnation restart; SQLite preserves v1.
  See [Durable Control-Plane Foundation](durable-control-plane.md).

## Current Limits

- The SQLite worker v1 registry, assignment generation, and tokens remain
  process-local transition fences. Production PostgreSQL uses v2 durable
  leases, but its scheduler is still round-robin and not leader-elected.
- Assignment is round-robin, not capacity-aware.
- The default trusted-lab path accepts caller-supplied worker identity. The production proxy path verifies mTLS certificate identity. Strict versioned reports are the default; `--allow-legacy-worker-reports` remains unsuitable for production.
- PostgreSQL v2 report aggregation commits scoped probe and catalog-monitor
  observations, pending/current alarm state, immutable alarm edges, and outbox
  records atomically. It exposes a bounded direct PostgreSQL monitor read model;
  an inbox-deduplicated JetStream consumer/replay projection remains open.
- The independent v2 heartbeat has authenticated identity and renews only
  unexpired active leases for its incarnation. Worker-health alarms and
  retry/backoff telemetry are not implemented yet.
- Durable tenant keys exist, but authorization grants and tenant-isolation behavior are not implemented.
- The app, PostgreSQL, and NATS deployments remain single instances. HA storage and leader election are not implemented.

## Next Upgrade Points

- Deploy JetStream consumers that replay immutable PostgreSQL monitor source
  results through `consumer_inbox` without changing the direct read authority.
- Add worker health/capacity metadata and capacity-aware assignment decisions.
- Connect structured security audit events to the durable audit repository.
- Split generated feed runtime out of the master when generated feeds need to scale independently from the GUI/API.

These upgrade points are necessary but not sufficient for production. The audit
requires authenticated and bounded APIs, durable fenced leases, idempotent
result ingestion, explicit unknown/stale semantics, backpressure, HA storage and
control-plane deployment, data-plane separation, and measured scale admission
gates before claiming support for thousands of streams.
