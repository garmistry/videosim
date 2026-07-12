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
3. Workers call `GET /api/workers/assignments?worker_id=<id>`.
4. The master returns running streams assigned to that worker. Assignment is currently a deterministic round-robin over active workers. The response includes worker API version, control-plane process instance, monotonic assignment generation, and an opaque per-worker assignment token.
5. An independent worker heartbeat refreshes membership while the worker reuses the existing monitor engine against its assigned streams.
6. Workers prune retained state to the current assignment and post `POST /api/workers/report` with the assignment contract, claimed stream IDs, monitor state, and bounded latest-batch probe metrics.
7. The master validates the instance/generation/token before mutation, derives authoritative scope from its current assignment, validates every alarm/event/pending/metric item, and reports rejected IDs explicitly.
8. The master merges accepted worker state into the shared monitor payload and exposes it through `/state.json`.

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
  startup on JetStream initialization. Backup/restore, restore generation
  fencing, and SQLite feed import tools exist.
- The integration evidence covers these repository primitives, not the current
  HTTP worker path. See [Durable Control-Plane Foundation](durable-control-plane.md).

## Current Limits

- The HTTP worker v1 registry, assignment generation, and tokens remain process-local and expire inactive workers after 60 seconds. They are transition fences, not the implemented-but-not-yet-wired durable lease repository.
- Assignment is round-robin, not capacity-aware.
- The default trusted-lab path accepts caller-supplied worker identity. The production proxy path verifies mTLS certificate identity. Strict versioned reports are the default; `--allow-legacy-worker-reports` remains unsuitable for production.
- HTTP report aggregation still writes the monitor JSON state file. Database-backed alarm/result methods exist but the handler and operator read projection have not cut over.
- The independent heartbeat has authenticated identity on the production proxy path, but no durable lease renewal, health alarm, or retry/backoff telemetry yet.
- Durable tenant keys exist, but authorization grants and tenant-isolation behavior are not implemented.
- The app, PostgreSQL, and NATS deployments remain single instances. HA storage and leader election are not implemented.

## Next Upgrade Points

- Cut worker assignments/reports from process-local v1 fencing to the durable incarnation/epoch/config/result contract.
- Add worker health and capacity metadata to assignment decisions.
- Move monitor report writes and operator reads from JSON to the PostgreSQL authority/projection, then deploy idempotent JetStream consumers.
- Connect structured security audit events to the durable audit repository.
- Split generated feed runtime out of the master when generated feeds need to scale independently from the GUI/API.

These upgrade points are necessary but not sufficient for production. The audit
requires authenticated and bounded APIs, durable fenced leases, idempotent
result ingestion, explicit unknown/stale semantics, backpressure, HA storage and
control-plane deployment, data-plane separation, and measured scale admission
gates before claiming support for thousands of streams.
