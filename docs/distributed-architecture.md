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
   fencing. PostgreSQL mode returns worker v2 durable offered/active lease
   assignments with lease epoch, config version, and expiry. Each durable poll
   reads the current external-feed catalog from PostgreSQL inside the scheduler
   transaction rather than trusting one API process's startup cache. Workers
   that advertise `capacity.maxStreams` receive capacity-aware assignments;
   unconfigured workers retain deterministic round-robin compatibility.
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
- `docker-compose.worker-domain.yml` renders 11 uniquely identified mTLS
  workers for one Compose/VM failure domain. Run the same immutable image on
  three separate hosts with distinct domain names, certificates, and spool
  storage for the checked-in F5 candidate. `worker-domain-startup.py` validates
  the candidate zone/certificate set, immutable image and container identity,
  host plus worker-path health APIs, bounded Docker Engine identity,
  zero-restart process stability, and logs; one local run is not three-host
  deployment evidence.
- Generated DASH assignments include a master HTTP `monitorEndpoint` so workers do not need a shared DASH volume.
- Generated SRT workers use `--srt-host` to reach listener feeds through the master/app container host name.
- Worker contract `videosim.worker/v1` adds process-restart, generation, and assignment-token fencing. HTTP 409 causes a bounded assignment refetch instead of silently applying stale state.
- Reports are server-scoped: caller-supplied stream IDs cannot grant ownership, retained state is pruned on workers, and malformed/out-of-scope state cannot replace another worker's alarms.
- Workers heartbeat independently of serial probe batches; the candidate default is five seconds against a configurable 30-second registry/lease freshness boundary.
- Durable workers may advertise `capacity.maxStreams` with `--max-streams`; the
  scheduler refuses new assignments beyond aggregate advertised capacity and
  reports the shortfall instead of silently over-admitting a worker.
- `capacity.maxSrtStreams` and `capacity.maxDashStreams` refine durable
  admission by protocol. A candidate must satisfy both total and matching
  protocol caps; uncapped workers retain compatibility behavior.
- Workers can run assigned streams through a bounded pool with
  `--max-concurrent-checks`; checks within one stream remain ordered and
  serial.
- Workers can apply `--stream-budget-seconds` so an over-budget stream reports
  timeout and does not start its remaining lower-priority checks. Existing
  alarms remain active on inconclusive timeout/error observations. The same
  budget caps built-in GStreamer, FFmpeg, FFprobe, and DASH polling/socket waits.
- Concurrent workers use one bounded two-phase pool and submit at most one
  phase-specific concurrency window at a time. `--max-concurrent-deep-checks`
  can cap TR-101/frame-rate/loudness streams below validation concurrency.
  Admitted validation finishes first; its results and original per-stream
  deadlines carry into the deep-check phase.
- `--batch-budget-seconds` stops new validation windows after a completed window
  exhausts the aggregate cycle budget. Deferred validation is inconclusive,
  preserves alarms, and rotates to the front of the next worker cycle; the deep
  phase also defers. This bounds the local executor queue, but in-flight probes
  still use their per-stream deadline and no durable fleet queue exists.
- Worker API v2 can write each exact fenced report to a Fernet-authenticated,
  fsynced local spool before send. A byte quota bounds disk use; blocked replay
  pauses new probes, startup replay precedes incarnation registration, accepted
  reports are deleted, and explicit `409` stale reports are discarded.
- Independent worker heartbeats persist a schema-bounded latest pressure
  snapshot inside durable capacity metadata. Assignment responses retain it and
  PostgreSQL `/state.json` exposes per-worker cycle-active, prior deferral/batch,
  current spool, completed-batch CPU, worker/child peak RSS, and Linux
  post-batch descriptor fields without adding unbounded metric labels.
- Durable assignment excludes workers reporting `spoolBlocked=true`. Healthy
  fresh lease owners are retained up to the current balanced total/protocol
  target, so worker loss moves only unavailable ownership while worker joins
  still rebalance. Insufficient or all-blocked capacity produces explicit
  shortfall instead of new authority on a worker that cannot deliver results.
- Each worker assignment poll reconciles its complete offer/renew set in one
  PostgreSQL transaction, and each lease-acknowledgement array commits in one
  transaction after full request validation. Stream locks use stable ID order;
  single-lease helpers retain the same epoch/config fences.
- Durable assignment takes a tenant-scoped PostgreSQL advisory leadership lock,
  reads and locks the current feed catalog, and performs worker/owner reads,
  offer/renew, and revocation in that same transaction. Concurrent replicas
  wait for the transaction lock instead of returning avoidable contention
  errors; loss of the database session rolls back the decision and releases the
  lock for the next replica.
- `GET /api/operator/feeds` is a versioned viewer-authorized read of the shared
  PostgreSQL feed catalog. It uses an ID cursor, defaults to 100 rows, caps a
  page at 200, returns config versions, and fails closed on an invalid durable
  row. SQLite lab mode provides the same bounded response from local state.
- Durable GUI bootstrap embeds only the first 100 catalog rows and no process
  stream array. `GET /api/operator/overview` returns capped alarms/events/
  pending rows and worker summaries without per-stream probe metrics. `GET
  /api/operator/feeds/<id>` reads one current configuration directly and scopes
  monitor rows to that feed. Generated detail checks a version-specific runtime
  ready lock through PostgreSQL and returns its current owner/session health;
  an absent lock remains explicit runtime-unknown state.
- PostgreSQL API startup does not preload durable feed rows into process state.
  Durable creates use UUID-backed feed IDs. Generated SRT writes serialize on
  the tenant row and allocate from a bounded range protected by a partial unique
  index; the exact 1,000-port range passes concurrent two-store coverage.
  SQLite retains sequential IDs and startup reloads.
- Durable configuration forms carry the displayed positive config version.
  Each API reconstructs only the requested durable feed and reuses the existing
  PostgreSQL compare-and-swap/audit transaction, so update, alert-profile,
  desired start/stop, and delete do not require process-cache routing. A
  separate generated runtime claims feed, port, and version-ready session locks
  only after launching media. Stale requests return HTTP 409.
- Latest-batch probe metrics classify success, issue, error, timeout, and skipped checks with monotonic durations. Metrics are replaced, assignment-scoped summaries rather than unbounded history.
- `python -m videosim control-plane-benchmark` exercises deterministic in-process
  assignment/report invariants and optional worker removal. It requires complete
  survivor coverage, rejects failed-worker stale reports, and reports avoidable
  placement churn while explicitly disclaiming durable or media capacity.
- `python -m videosim worker-benchmark` runs the real monitor/media-probe path
  for one worker from a hashed exported-state scenario. It reports cycle/CPU
  percentiles, aggregate/per-protocol outcomes, bounded fixture-behavior
  coverage and outcomes, unique validation-start coverage, conservative
  cycle-boundary wall-time freshness bounds, peak RSS, and descriptors, but
  makes no fleet-capacity claim.
- `python -m videosim fixture-fleet` writes benchmark-state scenarios for
  deterministic healthy, delayed, dead, and malformed DASH or SRT endpoints.
  It reuses the existing generators, a standard-library DASH HTTP service, and
  GStreamer SRT listeners. Optional per-behavior endpoint counts expand distinct
  URLs/listener ports; checked-in manifests declare 500 URLs per protocol. At
  scale, one captioned SRT encoder feeds local UDP multicast; each live listener
  has its own downstream-leaky relay process. Concurrent testing showed that
  multi-sink relay pipelines did not reliably accept repeated caller sweeps.
- `python -m videosim fixture-scenario` deterministically expands those states
  into exact logical protocol/behavior mixes for bounded-worker tests. Repeated
  protocol state options aggregate source-host shards, retain their hashes, and
  reject duplicate cross-shard endpoints. The checked-in 1,000-stream scenario
  reuses eight endpoints and is not a capacity workload.
- `python -m videosim import-fixture-scenario` validates that composed state,
  converts each entry to the existing complete external-feed configuration,
  and atomically reconciles a dedicated PostgreSQL catalog. It rejects shared
  endpoints and unrelated rows by default, verifies exact persisted parity,
  and emits a report that cannot certify media or capacity. Workers then read
  those real URLs through the normal scheduler/lease path.
- `docker-compose.fixture-domain.yml` runs one 220-SRT/220-DASH source shard
  on a Linux load host. Three uniquely advertised shards compose exact
  1,320/660/660 headroom input; the marked startup validator checks exact
  manifest/state endpoint inventory, DASH HTTP, sampled real SRT/DASH media,
  bounded Docker Engine identity, process state, Docker resources, and logs
  before a run. The host fault workflow stops both fixture services, proves
  sampled media failure, restarts them, and records recovery timing without
  claiming all-endpoint coverage.
- `durable-fixture-startup.py` composes one local source shard into an exact
  440-stream catalog, boots fresh PostgreSQL plus the API and 11 real workers,
  and fails closed on lease balance, latest behavior outcomes, alarm shape,
  paginated operator API parity, final fixture/application process state and
  resources, or Docker logs. A digest-pinned same-host run passes all 440 paths;
  its output remains explicitly one-domain, non-capacity startup evidence.
- `f5-domain-preflight.py` verifies the artifacts copied from three fixture and
  three worker domains before the distributed run. It requires exact candidate
  endpoint/behavior/worker/zone shape, state-hash parity, required startup
  checks and windows, distinct advertised hosts, six distinct bounded Linux
  Docker Engine identities, and one immutable image digest. Daemon identity
  prevents hostname-only reuse but cannot attest physical host topology; the
  preflight never certifies physical independence or capacity.
- `python -m videosim capacity-check` verifies a versioned scale workload and
  immutable evidence bundle against a policy. It fails on missing baseline
  criteria/artifacts, insufficient declared duration/headroom/survivor tokens,
  dirty or skipped runs, path escape, and SHA-256 drift; it runs no workload.
- `python -m videosim control-plane-load` drives the exact candidate shape in
  disposable PostgreSQL. Its declared endpoint storm commits separate
  all-stream `feed_reachable` unhealthy and healthy windows and verifies alarm
  plus outbox parity. Those inputs are synthetic and do not replace the
  fixture-domain media fault required for admission.

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
  leases, a transaction-consistent external-feed catalog, and database
  transaction-scoped scheduler leadership. Bounded GUI catalog, overview, and
  single-feed reads can use any API replica, API restart does not preload the
  catalog, and durable create IDs do not depend on replica-local state. Runtime
  state remains known only to a process with the matching local config version;
  update/delete mutations and generated-feed process ownership remain local to
  one API process. No continuously elected scheduler or replicated API
  deployment exists.
- PostgreSQL assignment is capacity-aware for workers advertising total or
  SRT/DASH protocol limits; unconfigured workers use compatibility round-robin.
  A current spool-blocked signal removes a worker from placement, but has no
  hysteresis or durable queue. Admission counts remain static operator values,
  not weighted cost or media capacity.
- Worker concurrency and built-in probe waits are bounded when configured, but
  black/frozen validation can still be expensive. Arbitrary checker/trickling-
  HTTP preemption, weighted check-cost/tenant tokens, pressure history/alerts/
  recovery SLOs, spool key rotation/repair, and durable fleet-queue backpressure
  remain open. Workers bound local probe submissions to one phase-specific
  concurrency window, rotate validation deferred by aggregate budget pressure,
  shed the deep phase, and stagger deep retries at stable per-stream offsets.
  The separate validation/deep tokens share one executor and are not measured
  weighted cost. Graceful workers finish
  the current report, stop
  heartbeats, and transition their fenced incarnation/leases to `draining` for
  immediate higher-epoch reassignment; hard kills still rely on lease expiry.
- The default trusted-lab path accepts caller-supplied worker identity. The production proxy path verifies mTLS certificate identity. Strict versioned reports are the default; `--allow-legacy-worker-reports` remains unsuitable for production.
- PostgreSQL v2 report aggregation commits scoped probe and catalog-monitor
  observations, pending/current alarm state, immutable alarm edges, and outbox
  records atomically. It exposes a bounded direct PostgreSQL monitor read model;
  an inbox-deduplicated JetStream consumer/replay projection remains open.
- The independent v2 heartbeat has authenticated identity and renews only
  unexpired active leases for its incarnation. Worker-health alarms and
  retry/backoff telemetry are not implemented yet.
- Durable tenant keys exist, but authorization grants and tenant-isolation behavior are not implemented.
- The app, PostgreSQL, and NATS deployments remain single instances. Replicated
  API routing, persistent scheduler leadership, and HA storage are not implemented.
- The candidate worker-domain startup path has passed locally against a durable
  API with 11 active workers, but it used a mutable local image and HTTP path
  and is not host-sized. Same-host three-domain loss/partition and two-API
  assignment smokes exist, but no independent-host HTTPS/mTLS deployment,
  production load balancer, HA storage, or production recovery artifact exists.
- The fixture-domain Compose contract and sampled startup path are implemented;
  one local Docker VM booted an exact 220+220 shard and sequential one-worker
  sweeps attempted all 220 distinct paths for each protocol with behavior-level
  outcomes. The shape has not run on three independent hosts or under the full
  1,320-stream worker/alarm load. Each shard still shares one media generator
  per protocol, so independent-source and capacity evidence remain open.

## Next Upgrade Points

- Add client-supplied config-version preconditions and runtime-owner routing
  before enabling update/delete mutations across API replicas.
- Deploy JetStream consumers that replay immutable PostgreSQL monitor source
  results through `consumer_inbox` without changing the direct read authority.
- Add worker health/utilization metadata, bounded concurrent execution,
  cancellation, spool key rotation/repair, durable fleet-queue backpressure, and
  scheduling beyond the current static capacity and local batch-pressure controls.
- Connect structured security audit events to the durable audit repository.
- Split generated feed runtime out of the master when generated feeds need to scale independently from the GUI/API.

These upgrade points are necessary but not sufficient for production. The audit
requires authenticated and bounded APIs, durable fenced leases, idempotent
result ingestion, explicit unknown/stale semantics, backpressure, HA storage and
control-plane deployment, data-plane separation, and measured scale admission
gates before claiming support for thousands of streams.
