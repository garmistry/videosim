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
- Storage boundary: feed definitions stay behind the `FeedRegistrationStore` interface so SQLite can be replaced without touching GUI orchestration or monitor logic.

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

## Current Limits

- Worker registry, assignment generation, and tokens are process-local and expire inactive workers after 60 seconds. They are transition fences, not durable HA leases.
- Assignment is round-robin, not capacity-aware.
- Worker identity is still caller supplied. Strict versioned reports are the default; `--allow-legacy-worker-reports` is an explicit temporary compatibility escape hatch that lacks stale-generation protection.
- Report aggregation still writes the monitor JSON state file; alarm/event history is not database-backed or append-only yet.
- The independent heartbeat has no authenticated identity, health alarm, or retry/backoff telemetry yet.
- No worker authentication, TLS, or tenant isolation.
- The master is still a single process. HA control-plane storage and leader election are not implemented.

## Next Upgrade Points

- Persist workers, leases, assignments, and alarm/event history through a database-backed repository boundary.
- Add worker health and capacity metadata to assignment decisions.
- Move monitor report writes from the JSON file to the same DB boundary used by feed registrations.
- Add signed worker credentials before this runs outside a trusted lab network.
- Split generated feed runtime out of the master when generated feeds need to scale independently from the GUI/API.

These upgrade points are necessary but not sufficient for production. The audit
requires authenticated and bounded APIs, durable fenced leases, idempotent
result ingestion, explicit unknown/stale semantics, backpressure, HA storage and
control-plane deployment, data-plane separation, and measured scale admission
gates before claiming support for thousands of streams.
