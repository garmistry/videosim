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
4. The master returns running streams assigned to that worker. Assignment is currently a deterministic round-robin over active workers.
5. Each worker reuses the existing monitor engine against its assigned streams.
6. Workers post `POST /api/workers/report` with `workerId`, assigned `streamIds`, and monitor state.
7. The master merges that worker state into the shared monitor payload and exposes it through `/state.json`.

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

## Current Limits

- Worker registry is in memory and expires inactive workers after 60 seconds.
- Assignment is round-robin, not capacity-aware.
- Report aggregation still writes the monitor JSON state file; alarm/event history is not database-backed yet.
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
