# Production-Readiness and Thousand-Stream Scaling Audit

**Date:** 2026-07-11

**Status:** Static, evidence-backed audit; not a capacity certification

**Audited implementation baseline:** Git commit `df8e8f235a72b643f130c3164420a9a83508ad17`; the audit document and its related documentation edits were uncommitted overlays during inspection.

**Decision:** The current distributed slice can be used for isolated, non-authoritative development evaluation only when exactly one monitoring ownership path is enabled and the network is trusted. It is not production-ready for 1,000 or more monitored streams.

**Post-audit implementation note:** this file preserves the audited baseline and
its original conclusions. F0, the core F1 boundary, and the first F2
PostgreSQL/JetStream repository unit were implemented afterward. Their current
evidence and remaining gates are tracked in
[`distributed-implementation-progress.md`](distributed-implementation-progress.md)
and [`durable-control-plane.md`](durable-control-plane.md); none changes the
no-capacity-certification decision above.

## 1. Scope, evidence, and claim labels

This audit evaluates how Video Feed Simulator could reliably monitor 1,000, 5,000, and 10,000 concurrent SRT/DASH streams with a highly available control plane and horizontally scalable workers. It does not implement that redesign.

Claims use these labels:

- **Confirmed:** directly supported by repository code, tests, configuration, or documentation.
- **Modeled:** arithmetic based on explicit assumptions, not measured capacity.
- **Unknown:** requires a product decision, representative infrastructure, or benchmark.
- **Target:** proposed production behavior, not current behavior.

Repository paths and line ranges refer to the audited implementation baseline above. Source inspection included the clean committed implementation plus the documentation overlays listed in Section 15; unrelated pre-existing untracked paths were excluded. The primary evidence is:

- Control plane and feed runtime: `videosim/gui.py:242-267, 456-608, 794-807, 920-934, 1282-1380, 1532-1632`.
- Worker loop: `videosim/worker.py:12-58`.
- Monitor execution and state: `videosim/monitor.py:67-72, 203-218, 233-259, 333-410`.
- Registration persistence: `videosim/feed_store.py:11-150`.
- SRT/DASH validation: `videosim/validator.py:33-107, 199-247, 299-368`.
- Browser polling: `frontend/src/main.jsx:67-115`.
- Distributed runtime topology: `docker-compose.yml:1-37`; test/soak services and volumes: `docker-compose.yml:38-78`.
- Current distributed contract: `docs/distributed-architecture.md`.
- Current limitations and missing evidence: `KNOWN_LIMITATIONS.md`, `TEST_GAPS.md`, and `docs/stability-report.md`.
- Existing behavior tests: `tests/test_worker.py`, `tests/test_gui.py`, `tests/test_monitor.py`, and `tests/test_feed_store.py`.

No 1,000-stream load environment, representative stream inventory, production credentials, multi-zone deployment, or measured per-check resource profile was available. Consequently, all capacity numbers below are models and explicit benchmark hypotheses.

## 2. Requirement-to-evidence matrix

| Audit requirement | Evidence/artifact in this document |
|---|---|
| Current architecture and end-to-end trace | Sections 3 and 4; repository line references |
| Production risks and named bottlenecks | Section 5 risk register |
| Target architecture and explicit assumptions | Sections 6 and 7 |
| Leases, fencing, retries, deduplication, versioning, reconciliation | Sections 6.3-6.6 |
| Storage, messaging, API, security, observability, deployment, HA | Sections 6, 8, and 9 |
| SRT/DASH and generated/external distinctions | Section 10 |
| Capacity models for 1k/5k/10k | Section 7, including formulas and benchmark gaps |
| Failure-mode/FMEA | Section 11 |
| Phased migration, compatibility, rollback, gates | Section 12 |
| Verification plan and measurable acceptance | Section 13 |
| Confirmed vs modeled vs unknown claims | Claim labels throughout; Sections 1 and 7 |
| Completion evidence and blocked claims | Sections 14 and 15 |
| Preserve MVP behavior; audit-only, no redesign/dependencies | Documentation-only diff audit in Section 15; repository regression commands |
| Primary evidence and material-claim citations | Sections 1, 3-5, and Section 15 attestation ledger |
| Related architecture/limitations consistency | `docs/distributed-architecture.md` and `KNOWN_LIMITATIONS.md` links/disclaimers |
| Work log, verification, and task-only commit | `docs/work-log.md`; Section 15 evidence manifest; final Git commit |
| Iterative subsystem validation and independent review | Section 15 inspection/review evidence |

## 3. Current architecture

### 3.1 Process and storage topology

**Confirmed.** `GuiState` is the control plane, GUI model, local feed orchestrator, and worker registry in one Python process. It owns feed records, local `subprocess.Popen` handles, logs, validation output, worker timestamps, and process-local locks (`videosim/gui.py:242-267`). `ThreadingHTTPServer` exposes browser and worker endpoints using that shared object (`videosim/gui.py:1620-1632`). Generated feeds are local subprocesses with a log thread, and stop signals their local process groups (`videosim/gui.py:458-523, 637-665`).

Feed definitions and alert profiles are persisted through `FeedRegistrationStore`; the only implementation is one SQLite connection guarded by an `RLock`, committing each mutation (`videosim/feed_store.py:11-19, 30-102`). Process handles, runtime state, assignments, workers, check results, alarms, and events are not in that database.

Monitor state is a complete JSON document. The standalone monitor writes it through a temporary-file replacement (`videosim/monitor.py:67-72`). Worker report ingestion reads, filters, extends, and rewrites the same logical document under a lock that protects only the GUI process (`videosim/gui.py:1565-1613`). Compose can run both a global `monitor` and a `worker` beside the app (`docker-compose.yml:15-37`), so there is no single authoritative owner when both are enabled.

### 3.2 Feed registration and GUI/API flow

1. The browser sends create/update/delete/start/stop actions to the same HTTP handler that serves the GUI (`videosim/gui.py:794-945`).
2. Feed mutations update `GuiState` and synchronously persist through the SQLite store (`videosim/gui.py:296-336, 357-433`; `videosim/feed_store.py:52-102`).
3. Starting a generated feed launches GStreamer locally; stopping it signals the local process group (`videosim/gui.py:458-523, 637-665`). A second API replica could not control that process because the handle is process-local.
4. `/state.json` walks the stream collection and returns stream state, recent logs, metrics, and monitor data (`videosim/gui.py:1282-1337, 1532-1548`).
5. Every browser fetches the complete payload once per second. If any preview is available, the browser also changes preview URLs every 1.8 seconds; five-minute graph history exists separately in each browser (`frontend/src/main.jsx:67-115`).

### 3.3 Worker registration and assignment flow

1. A worker calls `GET /api/workers/assignments?worker_id=...` with a caller-selected identity (`videosim/worker.py:16-19`; `videosim/gui.py:801-807`).
2. That GET updates an in-memory last-seen timestamp. Workers expire after 60 seconds (`videosim/gui.py:52-53, 1362-1375`). There is no independent heartbeat while probes run.
3. Active worker IDs and running stream IDs are sorted. Assignment is `stream_index % active_worker_count`, so membership changes can remap a large fraction of streams (`videosim/gui.py:1340-1349`). Capacity, protocol cost, locality, draining, stickiness, and current utilization are not inputs.
4. Generated DASH assignments point back to the master-served manifest; generated SRT configuration replaces the endpoint hostname with the worker's configured SRT host (`videosim/gui.py:1352-1358`; `videosim/monitor.py:80-101`).

### 3.4 Monitoring and report flow

1. The worker retains an in-memory monitor state across loops, fetches all assignments, calls `run_monitor_once`, posts the full retained state, then sleeps (`videosim/worker.py:34-58`). Any uncaught network or parsing error terminates the loop.
2. `run_monitor_once` iterates assigned streams serially. It can execute validation, TS sampling/TR 101 290, frame-rate probing, and loudness measurement for each stream (`videosim/monitor.py:333-373`). These checks spawn GStreamer/FFmpeg/FFprobe processes in several paths; SRT TS sampling alone creates a process and waits for the sampling timeout (`videosim/monitor.py:203-218`).
3. Some checker exceptions are discarded (`videosim/monitor.py:358-370`). Because alarm reconciliation clears issues absent from the next issue set (`videosim/monitor.py:233-259`), probe failure can be indistinguishable from recovery for those paths.
4. The worker POST has `workerId`, a caller-provided list of `streamIds`, and complete state, but no lease ID, assignment generation, sequence, idempotency key, or configuration version (`videosim/worker.py:22-31`).
5. The master trusts those fields, removes existing state for the claimed streams, appends all submitted arrays without checking each item's stream scope, and rewrites the JSON file (`videosim/gui.py:928-934, 1565-1582`). A stale or unauthorized worker can overwrite authoritative state. There is also a normal-operation integrity defect: a worker retains monitor state across assignment changes (`videosim/worker.py:43-55`), reports only its current stream IDs, and can therefore re-append stale records for streams it no longer owns—including after receiving an empty assignment.
6. The GUI reads that file and exposes only the last 100 alarms, 200 events, and 100 pending entries (`videosim/gui.py:1532-1548`). This is truncation, not queryable fleet-scale history.

### 3.5 Existing verification surface

**Confirmed.** Tests cover the first slice, not production scale:

- `tests/test_worker.py` mocks one successful fetch/monitor/report iteration.
- `tests/test_gui.py` checks basic two-worker distribution, generated DASH endpoint rewriting, and scoped report merging.
- `tests/test_monitor.py` exercises alarm, profile, and fixture behavior.
- `tests/test_feed_store.py` checks SQLite round trips and a threaded write.

There is no repository test proving authenticated access, lease fencing, stale-report rejection, worker failure/reassignment, multi-replica consistency, bounded concurrency, backpressure, queue recovery, 1,000-stream throughput, event storms, restore, or control-plane failover.

## 4. Current data-flow diagram

```text
Browser(s)
  |  full /state.json every 1 s; mutation forms
  v
Single ThreadingHTTPServer + shared GuiState
  |-- SQLite: feed definitions + alert profiles only
  |-- local Popen: generated SRT/DASH feeds
  |-- local/shared JSON: alarms + events + pending + workers
  |-- DASH files served from app process
  |
  +<-- GET assignments -- Worker(s)
  +<-- POST full report -- Worker(s)
                           |
                           +-- serial stream loop
                               +-- validator subprocesses
                               +-- GStreamer TS sample
                               +-- FFprobe frame-rate
                               +-- FFmpeg loudness

Optional standalone monitor --------------------> same monitor JSON domain
```

This combines operator API, scheduler, worker registry, report ingestion, alarm projection, generated-feed lifecycle, DASH data serving, and browser reads in one failure and scaling domain.

## 5. Production risk register

| Severity | Risk and confirmed evidence | Production impact | Required treatment |
|---|---|---|---|
| Blocker | Single-process control plane and process-local feed handles (`videosim/gui.py:242-267, 1620-1632`) | No safe horizontal API scaling or HA; restart loses ownership | Stateless replicated APIs; durable desired/observed state; separate feed runtime |
| Blocker | Unauthenticated worker/operator endpoints and caller-trusted report scope (`videosim/gui.py:801-807, 920-934, 1565-1582`) | State corruption, data exposure, SSRF/DoS, cross-tenant access | AuthN/AuthZ, mTLS, schema/body/rate limits, ownership validation, audit trail |
| Blocker | Serial, synchronous, subprocess-heavy worker loop (`videosim/worker.py:34-58`; `videosim/monitor.py:333-373`) | Unknown and likely highly variable capacity; one slow stream delays all | Tiered scheduling, bounded concurrency, deadlines, cancellation, cost tokens, benchmarks |
| Blocker | No durable lease, epoch, fencing, ack, or independent heartbeat (`videosim/gui.py:1340-1375`) | Duplicate authoritative monitoring, stale writes, broad reassignment churn | Durable leases with epochs, CAS, separate heartbeat, drain/reconcile loops |
| Blocker | Full JSON report read/modify/write (`videosim/gui.py:1565-1613`) | Serialized O(state) writes, lost updates across processes, no retention/query model | Transactional DB projection plus durable event ingestion |
| Blocker | Retained worker state is appended without validating item scope against current assignments (`videosim/worker.py:43-55`; `videosim/gui.py:1565-1577`) | Ordinary rebalance/empty assignment can reintroduce duplicates, stale alarms, and false authority | Reject out-of-scope items immediately; ultimately fence every result by durable lease epoch |
| High | Modulo round-robin ignores capacity and remaps broadly (`videosim/gui.py:1340-1349`) | Overloaded workers, cache/probe churn, incident amplification | Stable, capacity-aware scheduling with protocol/check cost |
| High | Worker has fixed 10-second HTTP timeout and no retry/backoff/spool (`videosim/worker.py:16-58`) | Transient control-plane errors terminate monitoring and lose reports | Jittered retry, circuit breaker, bounded durable spool, health state |
| High | Checker exceptions can be swallowed (`videosim/monitor.py:358-370`) | False alarm clearing and hidden worker/tool failure | `healthy/unhealthy/unknown/stale`; probe self-health; never clear on unknown |
| High | Optional standalone monitor and workers overlap (`docker-compose.yml:15-37`) | Duplicate checks and cross-process lost updates | One ownership model; disable legacy path after migration gate |
| High | Generated DASH assignments use master URLs and the GUI handler reads each requested file into memory (`videosim/gui.py:1002-1031, 1352-1358`) | Data-plane load competes with scheduling/report/API load | Dedicated origin/object store; streaming/range support; separate feed service |
| High | SQLite single connection and per-write commits (`videosim/feed_store.py:30-102`) | Single-host write serialization; no HA/multi-replica contract | Production relational DB and repository implementation |
| High | External endpoints pass only basic scheme/shape checks before becoming worker fetch targets (`videosim/gui.py:118-135`; `videosim/monitor.py:80-101`) | SSRF and network pivot risk; unbounded egress | Tenant-scoped allowlists, URL validation, DNS/IP re-checks, egress policy |
| Medium | Full browser state poll every second (`frontend/src/main.jsx:67-115`) | O(users × streams) serialization/network load; stale/truncated UX | Paginated APIs, filtered views, delta/event updates, cache/read model |
| Medium | Browser preview refresh is 1.8 seconds and each generated preview launches GStreamer (`frontend/src/main.jsx:86-92`; `videosim/gui.py:1392-1449`) | Client-driven media process amplification | On-demand throttled preview service, cache, quotas, disable at fleet view |
| Medium | Alarm/event arrays are truncated (`videosim/gui.py:1532-1548`) | Operators cannot query or audit fleet incidents | Indexed/paginated APIs, durable history, retention, acknowledgements |
| Medium | Compose exposes only UDP 9000-9010 (`docker-compose.yml:7-10`) | Generated SRT beyond 11 published ports is unreachable | Separate generated-feed nodes and explicit network/port allocation |
| Medium | Metrics are configured estimates, client history is ephemeral (`KNOWN_LIMITATIONS.md`) | Capacity and incident decisions use misleading data | Actual counters, centralized time series, labels with bounded cardinality |
| Medium | Compose defines one app/monitor/worker and no production health, replica, load-balancer, or multi-zone policy (`docker-compose.yml:1-37`) | Unsafe rollout and recovery | Readiness/liveness, disruption budgets, rollout/rollback, multi-zone policy |

## 6. Target production architecture

### 6.1 Principles and boundaries

**Target.** Separate desired state, scheduling, execution, result ingestion, and operator queries. The control plane must never carry media bytes in its normal path.

```text
Operator/UI -> API gateway -> replicated control-plane API
                                |-> relational DB (source of truth)
                                |-> scheduler/reconciler
                                |-> query/read model
                                |-> audit log

Workers <--- assignments/leases --- scheduler
Workers ---- heartbeats/capacity --> control plane
Workers ---- idempotent results ---> durable ingestion -> consumers
                                              |-> current check/alarm projection
                                              |-> append-only events
                                              |-> metrics/logs/traces

SRT/DASH endpoints <---- media probes ---- Workers
Generated feed nodes ---- SRT listeners / DASH origin-object storage
```

Control-plane responsibilities:

- Authenticate operators and workers; authorize every tenant/resource operation.
- Store versioned stream configuration, alert profiles, workers, capabilities, leases, current results, alarm state, and auditable events.
- Own alarm transitions centrally: workers submit observations/evidence but never authoritatively raise or clear alarms; an idempotent projection applies alert policy to fenced current results and emits append-only transitions.
- Reconcile desired monitoring coverage with healthy capacity.
- Issue fenced leases and reject stale results.
- Expose bounded, paginated, filtered APIs and fleet summaries.
- Manage retention, schema versions, migrations, rate limits, and rollout compatibility.

Worker responsibilities:

- Advertise protocol/check capabilities, capacity tokens, software version, zone/region, and health.
- Heartbeat independently of probe execution.
- Accept leases, honor deadlines, execute bounded concurrent checks, and report typed results.
- Apply backpressure, preserve a bounded encrypted local spool during control-plane interruption, and shed lower-priority checks before critical reachability checks.
- Drain gracefully during deployment and stop producing authoritative results when a lease expires.

Generated-feed runtime responsibilities:

- Run and expose generated SRT/DASH feeds independently of the API.
- Publish observed runtime health and endpoint metadata.
- Use explicit port/IP allocation for SRT; serve DASH from a dedicated origin or object store.

### 6.2 Data stores and messaging

**Recommended baseline:** a PostgreSQL-compatible HA relational database for authoritative state and transactional fencing, plus a durable broker/stream for high-volume result ingestion. This is a recommendation, not an implementation mandate.

The relational database is preferable to the current SQLite/JSON combination because leases require atomic compare-and-set semantics, API replicas need shared state, and operators require indexed queries, constraints, migrations, backup, and point-in-time recovery. A managed HA PostgreSQL offering reduces operational burden. An alternative distributed SQL database is justified only if multi-region active/active writes are a proven requirement; it adds latency and operational complexity that the current product has not established.

A durable broker decouples worker report bursts from alarm projection and absorbs transient consumer or database slowdowns. Kafka-compatible streams suit high sustained event rates and replay; a managed queue such as SQS/Pub/Sub is simpler when ordering needs are per-stream and throughput is moderate. Do not select one until the measured report size/rate, replay window, ordering contract, and operational platform are known. The lease heartbeat/assignment path remains database/control-plane driven; the broker must not be the sole lease authority.

Minimum logical records:

- `tenants`, `users/service_accounts`, roles and grants.
- `streams` with immutable ID, tenant, protocol, endpoint reference, desired state, and `config_version`.
- `workers` with identity, capabilities, software version, capacity, zone, state, and last heartbeat.
- `leases` with stream, worker, `lease_epoch`, issue/expiry time, desired check tier, acknowledged time, and state.
- `check_results` keyed by stream/check/config version/lease epoch/result sequence.
- `current_check_state` with status `healthy|unhealthy|unknown|stale`, observed time, expiry, and evidence pointer.
- `current_alarms` and append-only `alarm_events` with stable event IDs.
- `audit_events` for operator and control-plane mutations.
- Outbox/inbox or equivalent transaction records for reliable publish and deduplication.

Partition high-volume event/results tables by time and, where useful, tenant hash. Define retention and deletion policy before production. Keep current state compact; archive detailed evidence to object storage when payloads are large.

### 6.3 Worker lifecycle, leases, and fencing

1. Worker boots with a cryptographic service identity and registers capabilities/capacity/version.
2. A heartbeat runs on a dedicated timer, independent of probes, and reports token utilization, queue depth, oldest task age, tool health, and drain state.
3. Scheduler creates or renews a durable lease with a monotonically increasing `lease_epoch`. Assignment is stable and capacity-aware; protocol/check costs consume worker tokens.
4. Worker acknowledges the lease and configuration version before it becomes authoritative.
5. Results include tenant, stream, check ID, `config_version`, `lease_epoch`, worker ID, result ID, per-lease sequence, start/end timestamps, status, and bounded evidence.
6. Ingestion validates ownership and accepts a state-changing result only if its epoch/config match the current lease/config. Duplicate result IDs are acknowledged without reapplying. Older epochs are retained only as diagnostic late data or rejected.
7. Worker renews before expiry. If renewal fails, it may finish work but must not claim authority after expiry. The reconciler waits a bounded grace period, increments the epoch, and reassigns.
8. Drain stops new leases, completes or relinquishes existing work by deadline, flushes the spool, and reports completion.

This prevents an old or partitioned worker from clearing alarms after reassignment. Exactly-once delivery is not required; **at-least-once transport plus idempotent, fenced application** is the practical contract.

### 6.4 Scheduling and backpressure

Use a reconciler rather than recomputing all ownership on every GET. Candidate selection should consider:

- Available CPU/memory/file-descriptor/network/process tokens.
- Protocol and check tier cost (reachability is cheaper than 10-second frozen/loudness analysis).
- Zone/region affinity to endpoints and failure-domain spread.
- Worker capability/tool versions.
- Existing stable ownership and drain state.
- Tenant quotas and noisy-neighbor limits.

Stable rendezvous hashing can reduce movement, but durable assignment with a least-loaded/cost-aware candidate is easier to reason about for heterogeneous checks. Rebalance only when utilization or failure requires it; cap moves per interval.

Backpressure policy, in order:

1. Reject or queue new low-priority leases when capacity is unavailable.
2. Jitter schedules to avoid synchronized probes.
3. Preserve reachability and critical essence checks; reduce expensive analysis cadence.
4. Bound each worker's concurrent subprocesses, bytes/sec, open sockets, spool bytes, and per-tenant share.
5. Mark overdue checks `stale`/`unknown`; never report them healthy.
6. Expose queue age and dropped/deferred work as SLO-impacting metrics.

### 6.5 API and compatibility contract

Version operator and worker APIs independently. Use strict schemas with unknown-field policy, bounded arrays/strings/evidence, request IDs, idempotency keys, and explicit compatibility windows. Configuration changes increment `config_version`; old workers must reject unsupported config or receive only a backward-compatible projection.

Rolling upgrade sequence: deploy readers/consumers compatible with old and new schema, expand database schema, deploy writers, migrate/backfill, verify, then contract in a later release. Never combine destructive schema contraction with the first code rollout. Maintain at least one-version worker/control-plane compatibility or coordinate drain-and-upgrade by pool.

A periodic reconciler must compare desired streams, live leases, fresh results, and worker health; repair orphaned streams, duplicate leases, expired work, and stale projections. Reconciliation must be idempotent and observable.

### 6.6 Retry, deadline, and poison-data contract

The values below are proposed defaults to validate in Phase 0. A caller must never retry past its operation deadline, and all retry delays use full jitter.

| Operation | Retryable conditions | Non-retryable conditions | Budget/backoff | Idempotency/order | Terminal handling |
|---|---|---|---|---|---|
| Worker register/heartbeat | timeout, connection failure, 429, 502/503/504 | 400/401/403, unsupported API/version | 10 s request deadline; 1-30 s exponential backoff; continue until identity expires | worker incarnation + monotonic heartbeat sequence | mark control-plane disconnected; stop accepting new work; alert locally |
| Assignment watch/poll | timeout, 429, transient 5xx | auth/version/schema rejection | 10 s request; 1-30 s backoff; honor `Retry-After` | resume cursor/ETag; assignment generation monotonic | retain valid unexpired leases only; stale after expiry |
| Lease acknowledge/renew | timeout, 409 race, 429, transient 5xx | fenced epoch, expired lease, auth/config incompatibility | must complete before `expiry - safety_margin`; 0.25-5 s backoff | `(stream_id, lease_epoch, worker_incarnation)` | stop authority and cancel/declassify work at expiry |
| Result publish | timeout, 429, transient 5xx | stale/fenced epoch, invalid schema, unauthorized stream | per-result deadline bounded by result freshness; 0.25-30 s backoff | globally unique result ID + per-lease sequence; at-least-once | spool until TTL/size cap; stale/fenced result becomes diagnostic only |
| Spool replay | broker/API transient failure | corrupt record, invalid schema, revoked identity | bounded batches; back off on pressure; newest critical current-state results may bypass old diagnostic events | preserve per-stream sequence, deduplicate by result/event ID | quarantine poison record to bounded dead-letter store and emit worker-health alarm |
| Operator mutation | timeout, 429, transient 5xx only when client supplied idempotency key | validation, conflict requiring user decision, auth failure | bounded client retries, maximum 30 s | idempotency key + resource version/ETag | return typed conflict/error; never silently retry non-idempotent action |

Spool policy must define maximum bytes, maximum record age, encryption, per-tenant partitions, fsync policy, eviction order, disk-reserve threshold, and operator visibility. When full, workers shed expired diagnostics and low-priority evidence before current critical results; they never claim success for dropped work. Poison messages are quarantined with metadata and bounded evidence, not retried forever.

### 6.7 Minimum API resource and consistency contract

The exact transport may be REST plus long polling, gRPC, or a broker-backed assignment notification; semantics are mandatory regardless of transport.

| Resource/operation | Required semantics |
|---|---|
| `streams` and `alert-profiles` | Tenant-scoped CRUD, optimistic resource version/ETag, idempotent create, validated endpoint reference, audit event |
| `workers` | Register incarnation, capabilities/version/capacity; heartbeat; drain/revoke; server-authoritative identity |
| `assignments`/`leases` | Watch or cursor-paginated list, monotonic assignment generation, acknowledge/renew/relinquish, explicit expiry and epoch |
| `results` | Bounded batch publish, per-item acceptance/rejection, result ID/sequence/config version/lease epoch, stale/fenced response |
| `alarms` | Filter by tenant/stream/state/severity/time, stable cursor pagination, acknowledge/suppress with version and audit trail |
| `events` | Append-only time-ordered query, stable cursor, retention metadata; no destructive client-side clear as audit mechanism |
| `fleet summaries` | Precomputed/read-model counts and SLO state; never require serializing every stream for each UI poll |

Use a typed error envelope containing code, request ID, retryability, safe detail, and optional retry delay. Cursor pagination must use a stable sort key such as `(observed_at, immutable_id)` and have a documented snapshot/eventual-consistency contract. Reads of configuration and current lease ownership require read-after-write consistency; fleet summaries and historical search may be explicitly eventually consistent with measured lag. Maximum request, batch, page, evidence, and response sizes are API configuration with tested hard caps. Worker assignment delivery may use a streaming channel, but workers must resume from a generation/cursor after disconnect and reconcile against the authoritative lease API.

## 7. Capacity model

### 7.1 Explicit assumptions

The following are **modeled starting hypotheses**, not measured facts:

| Variable | Baseline model | Status |
|---|---:|---|
| Concurrent streams | 1,000 / 5,000 / 10,000 | Requested scenarios |
| Critical reachability cadence | 15 s | Product decision required |
| Standard essence/metadata cadence | 60 s | Product decision required |
| Expensive black/frozen/loudness cadence | 300 s | Product decision required |
| Checks per stream by tier | 1 critical, 3 standard, 2 expensive | Inventory must be finalized |
| Average result payload | 2 KiB | Unmeasured estimate |
| Average event payload | 1 KiB | Unmeasured estimate |
| Average healthy alarm transition rate | 0.01 events/stream/min | Unmeasured estimate |
| Failure-storm transition rate | 2 events/stream/min | Test hypothesis |
| Worker usable utilization | 60% | Headroom policy proposal |
| Worker effective check capacity | unknown | Must be benchmarked by protocol/tier |
| Lease renewal interval | 15 s | Scheduling-model proposal; validate against failover SLO |
| Result retention | 7 days hot, longer aggregate/archive unknown | Product/compliance decision required |
| Broker replication factor | 3 | HA modeling assumption |
| Availability objective | 99.9% control-plane API monthly; 99.95% monitoring coverage | Product/SRE approval required |
| Detection SLO | 95% critical failures detected within 30 s | Product/SRE approval required |
| Reassignment SLO | 95% within 45 s, 99% within 90 s | Product/SRE approval required |
| Result freshness | 99% critical results younger than 30 s | Product/SRE approval required |
| DR target | RPO <= 5 min, RTO <= 60 min | Business approval required |

The current implementation's nominal five-second sleep is not a five-second per-stream cadence because sleep starts only after the serial batch completes (`videosim/worker.py:43-58`). Validator and media-tool timeouts make unhealthy endpoints potentially much more expensive than healthy ones (`videosim/validator.py:199-247, 299-368`).

### 7.2 Formulas

For tier `i` and a candidate worker shape:

```text
check_rate_i = stream_count * checks_per_stream_i / cadence_seconds_i
aggregate_check_rate = sum_i(check_rate_i)

fleet_core_need = sum_i(check_rate_i * measured_cpu_seconds_per_check_i) / target_utilization
fleet_network_Bps = sum_i(check_rate_i * measured_bytes_per_check_i)
fleet_concurrency_need = sum_i(check_rate_i * measured_p95_wall_seconds_i)
fleet_fd_need = fleet_concurrency_need * measured_p95_fds_per_inflight_check
result_ingest_Bps = aggregate_check_rate * average_result_bytes

workers_by_cpu = fleet_core_need / usable_cores_per_worker
workers_by_network = fleet_network_Bps / safe_network_Bps_per_worker
workers_by_concurrency = fleet_concurrency_need / safe_inflight_limit_per_worker
workers_by_fd = fleet_fd_need / safe_fd_budget_per_worker
worker_count = ceil(max(workers_by_cpu, workers_by_network,
                        workers_by_concurrency, workers_by_fd))
worker_count_with_reserve = ceil(worker_count / (1 - failure_domain_capacity_fraction))
```

`measured_p95_fds_per_inflight_check` includes child-process pipes and sockets. `safe_*` capacities are benchmarked limits after OS/runtime reserves, not theoretical maxima. Worker count cannot be responsibly filled in until CPU seconds, wall time, bytes, sockets, and subprocess/FD use are measured separately for healthy, slow, unreachable, malformed, black, frozen, and loudness cases.

### 7.3 Scenario demand model

Using the baseline assumptions:

```text
per-stream checks/second = 1/15 + 3/60 + 2/300 = 0.1233
```

| Streams | Aggregate checks/s | Results/min | Result ingress at 2 KiB | Healthy events/min at 0.01 | Storm events/min at 2.0 |
|---:|---:|---:|---:|---:|---:|
| 1,000 | 123 | 7,400 | 0.24 MiB/s | 10 | 2,000 |
| 5,000 | 617 | 37,000 | 1.20 MiB/s | 50 | 10,000 |
| 10,000 | 1,233 | 74,000 | 2.41 MiB/s | 100 | 20,000 |

Additional modeled fleet demand, using a 15-second lease renewal, 2 KiB stored result, and three broker replicas:

| Streams | Lease renewals/s | Result rows/day | Raw result GiB/day (no indexes) | Broker GiB/day at 3 replicas |
|---:|---:|---:|---:|---:|
| 1,000 | 67 | 10.7 million | 20.3 | 61.0 |
| 5,000 | 333 | 53.3 million | 101.6 | 304.9 |
| 10,000 | 667 | 106.6 million | 203.2 | 609.7 |

These figures are deliberately conservative in one dimension and incomplete in others: storing every check result as a row may be unnecessary after aggregation, while indexes, WAL, metadata, retries, and evidence increase bytes. They exist to force a measured retention/downsampling design, not to prescribe it.

These ingress byte rates exclude protocol overhead, evidence blobs, metrics, retries, and replication. They show that raw control messages are not necessarily the dominant cost; media decoding, process startup, endpoint bandwidth, concurrency, and failure timeouts are likely dominant but remain **unknown**.

Control-plane/storage planning must also calculate:

```text
heartbeats_per_second = worker_count / heartbeat_interval_seconds
lease_renewals_per_second = stream_count / lease_renewal_interval_seconds
api_requests_per_second = operator_reads + worker_control_requests + ingestion_batches
result_rows_per_day = aggregate_check_rate * 86400
result_storage_per_day = result_rows_per_day * stored_result_bytes * index_overhead_factor
broker_retained_bytes = result_ingest_Bps * retention_seconds * replication_factor
alarm_event_storage_per_day = events_per_stream_per_day * stream_count * stored_event_bytes
evidence_object_bytes_per_day = evidence_rate * average_evidence_bytes * 86400
ui_egress_Bps = concurrent_users * page_refresh_rate * average_page_or_delta_bytes
monthly_cost = compute + database + broker + object_storage + network_egress + observability
```

For 1,000/5,000/10,000 streams, Phase 0 must populate a capacity workbook with every term, p95 and storm multipliers, index/replication overhead, retention, failure-domain reserve, and unit cost. Required outputs are DB reads/writes/IOPS and retained bytes, broker partitions/throughput/retention, API/heartbeat/lease rates, evidence-object growth, UI read/egress, worker network/media bandwidth, and estimated monthly cost. Unknown terms remain visibly blank rather than defaulting to zero.

### 7.4 Illustrative worker sensitivity—not sizing evidence

If benchmarking found one worker safely sustains 25, 50, or 100 mixed checks/s **after applying the 60% utilization ceiling** while meeting latency and failure-storm SLOs, the arithmetic floor would be:

| Streams | At 25 checks/s | At 50 checks/s | At 100 checks/s |
|---:|---:|---:|---:|
| 1,000 | 5 workers | 3 workers | 2 workers |
| 5,000 | 25 workers | 13 workers | 7 workers |
| 10,000 | 50 workers | 25 workers | 13 workers |

Add at least one failure-domain's spare capacity and round upward for zone spread. These figures deliberately do **not** claim that current workers achieve any listed throughput. Given serial execution and subprocess-heavy checks, they are benchmark scenarios only.

### 7.5 Benchmarks required to replace estimates

Measure a matrix of SRT/DASH × generated/external × healthy/slow/dead/malformed × enabled check tier. For every cell capture:

- p50/p95/p99 wall time and CPU seconds per check.
- Bytes read/written, peak RSS, file descriptors, sockets, and child-process count.
- Timeout/cancellation cleanup and orphan-process count.
- Results/sec and streams/worker at 40%, 60%, 80%, and saturation.
- Alarm correctness and freshness during 10%, 50%, and 100% endpoint failure storms.
- Queue age, deferred work, spool growth, and recovery time after control-plane outage.

Run 1,000 for at least 24 hours before admitting that scale. Treat 5,000 and 10,000 as separate gates; do not linearly extrapolate from 1,000 healthy streams.

## 8. Security and tenancy

Minimum production controls:

- OIDC/OAuth2 for operators; short-lived workload identity and mTLS for workers.
- RBAC plus tenant/resource authorization on every read and mutation, including report ingestion.
- Certificates and secrets from a managed secret system, automated rotation, no secrets in logs or assignment payloads.
- TLS in transit and encryption at rest for database, broker, object storage, backups, and local worker spool.
- Strict endpoint schemas, maximum body/evidence/history sizes, decompression limits, per-identity and per-tenant rate limits, replay protection, and idempotency keys.
- External endpoint SSRF defenses: allowed schemes, credential redaction, DNS resolution controls, rejection of metadata/control-plane/private ranges unless explicitly tenant-approved, revalidation after redirects, and network egress policy.
- Immutable audit records for login, feed/config/profile changes, assignment overrides, alarm acknowledgement/suppression, credential events, and administrative access.
- Tenant quotas for streams, check cost, bandwidth, reports, events, retention, and previews; tenant IDs included in every storage key and authorization decision.
- Threat modeling, dependency/container scanning, SAST, secret scanning, SBOM/signing, vulnerability response SLO, and penetration testing before external production.

The current plain `urlopen` worker transport and unauthenticated handlers do not provide production-grade versions of these controls (`videosim/worker.py:16-31`; `videosim/gui.py:801-807, 920-934`). External URLs do receive basic scheme/shape validation (`videosim/gui.py:118-135`), but there is no address/redirect/DNS/tenant egress policy.

## 9. Observability, HA, and operations

### 9.1 Metrics and SLOs

Export centrally aggregated, bounded-cardinality metrics:

- Control plane: request rate/latency/errors, auth failures, DB/broker latency, lease reconcile duration, assignment churn, stale leases, rejected stale reports, ingestion lag, outbox backlog.
- Worker: heartbeat age, active/capacity tokens, queue age, checks by outcome, p95 duration by protocol/check, timeouts, subprocesses, RSS/CPU/FD/socket/network use, spool bytes/age, drain state.
- Monitoring quality: fresh/unknown/stale stream counts, detection latency, alarm transition lag, duplicate results, deferred checks, false-clear prevention.
- Data plane: generated-feed runtime health, SRT connections/bytes/errors, DASH origin latency/errors/bytes.

Use structured logs with request/result/lease IDs, redaction, sampling, and tenant-safe fields. Trace assignment-to-result-to-alarm paths with OpenTelemetry-compatible context. Do not use stream ID as an unbounded metric label; keep per-stream detail in logs/query storage.

Proposed initial SLOs in Section 7 are unresolved product decisions. Alert on burn rate, stale coverage, queue age, lease duplication, report rejection, spool pressure, DB/broker saturation, and worker pool capacity—not merely process uptime.

### 9.2 HA deployment

- At least three control-plane API/scheduler instances across failure domains, behind a load balancer. APIs are stateless; scheduler leadership uses a database-backed lease or platform-native leader election.
- HA relational database with synchronous local failover, tested backups, point-in-time recovery, and documented RPO/RTO.
- Broker replicated across failure domains with retention sized for the maximum tolerated consumer outage.
- Worker pools spread across zones/regions with N+failure-domain capacity and anti-affinity.
- Readiness checks include DB/broker reachability and migration compatibility; liveness detects deadlock only. Use graceful shutdown and worker drain.
- Pod disruption budgets, topology spread, resource requests/limits, autoscaling on capacity tokens/queue age rather than CPU alone, and controlled rollout waves.
- Separate generated-feed service and DASH origin from the control plane.

A single active scheduler leader simplifies lease decisions; replicated APIs and consumers remain active. Scheduler failover must preserve durable epochs and avoid duplicate current leases.

### 9.3 Backup, restore, and disaster recovery

Back up authoritative database and configuration/audit data; configure broker replay retention and object-store lifecycle. Encrypt backups, test restore at least quarterly, and record recovery duration/data loss against RTO/RPO. A restore runbook must address leases from the old timeline: increment a global control-plane generation or expire/reissue all leases so pre-disaster workers cannot submit authoritative stale results.

## 10. Protocol and source-specific scaling

### 10.1 SRT

- SRT monitoring opens live connections and consumes media bytes; connection setup, socket/FD use, receiver timeout, encryption/passphrase handling, and NAT/firewall locality affect capacity.
- Current generated listeners use individually allocated UDP ports, while Compose publishes only 9000-9010 (`docker-compose.yml:7-10`). Thousands of generated feeds require dedicated feed nodes, explicit address/port allocation, security groups, and bandwidth accounting—not more ports on the control-plane container.
- Short receiver probes can disturb sources or consume receiver limits. Reuse a bounded long-lived ingest session only if one decode can safely fan out checks and freshness semantics remain explicit; otherwise rate-limit connection churn.
- Locate workers near SRT sources to avoid cross-region media cost and latency. Never route media through the control plane.

### 10.2 DASH

- DASH checks create HTTP request fan-out for MPDs and segments. Respect cache headers while ensuring freshness, cap manifest/segment size, bound redirects, and reuse HTTP connections.
- Generated DASH currently routes worker access back through the master (`videosim/gui.py:1352-1358`). Production should publish to a dedicated origin/object store, optionally behind a CDN, and provide signed, scoped access where needed.
- Schedule manifest-only reachability separately from segment/decode checks. Coordinate workers so many checks do not fetch the same large segment simultaneously; a regional cache or shared ingest layer may be justified after measurement.

### 10.3 Generated versus external streams

Generated-feed hosting is a data-plane workload with media encoding, port/origin management, receiver fan-out, and lifecycle orchestration. External monitoring is an egress/probe workload with SSRF and source-impact concerns. They must share stream configuration and alarm semantics but scale independently. A generated feed can be monitored by the same worker contract, yet its runtime must not reside in the control-plane process.

## 11. Failure-mode and effects analysis

Scores are qualitative: severity (S), likelihood (L), detectability difficulty (D), 1 low to 5 high. Re-score with operational data.

| Failure mode | Current effect | Target detection/containment | S/L/D | Required verification |
|---|---|---|---:|---|
| Control-plane process loss | GUI/API, registry, scheduler, local feed handles lost | Replicated API; leader failover; durable leases; workers continue until lease expiry/spool limit | 5/3/2 | Kill leader under load; meet API/reassignment SLO |
| Database loss/latency | Current SQLite is local single point | HA failover; bounded retries/circuit breaker; no split-brain scheduler; restore runbook | 5/2/3 | Primary failover, latency injection, PITR drill |
| Database poison/consumer defect | Bad migration or projection input can block writes/consumers | Schema constraints, quarantine, deploy pause, replayable source, repair tooling | 5/2/4 | Inject incompatible row/event and prove bounded isolation |
| Broker loss/backlog | No broker; reports fail/worker exits | Bounded encrypted spool, backpressure, replay and idempotency | 4/2/3 | Broker outage through retention window; recover without state corruption |
| Worker death | Timestamp expires after 60 s; broad modulo remap | Independent heartbeat, durable expiry, epoch increment, stable reassignment | 5/3/2 | SIGKILL workers; 95/99% reassignment bounds |
| Worker partition | Old worker may report after remap | Lease expiry and ingestion fencing reject old epoch | 5/3/3 | Asymmetric partition; prove no stale authoritative result |
| Duplicate worker identity | Caller-selected ID collides | Cryptographic unique identity and active session/incarnation ID | 4/2/3 | Start duplicate identity; reject or fence old incarnation |
| Stale lease/report | No epoch/sequence; can overwrite | CAS epoch/config checks and idempotent result IDs | 5/3/4 | Reorder/replay reports; state remains monotonic |
| Out-of-scope retained worker state | Rebalance/empty assignment can append old records outside claimed stream IDs | Ingestion validates every item against current lease and report scope | 5/4/4 | Assignment loss, empty assignment, replay; no stale/duplicate authority |
| Clock skew/time jump | Timestamp-based TTL can expire live workers or extend stale leases | Server-authoritative monotonic lease time; bounded skew observation; epochs remain primary fence | 4/2/4 | Skew/jump worker and control-plane clocks |
| Slow/dead stream | Serial timeout delays all assignments | Deadlines, bounded concurrency, per-tenant tokens, stale state | 4/4/2 | 10/50/100% slow endpoint storms |
| Malformed stream/tool crash | Exceptions may disappear; child risk | Sandbox/resource limits, explicit unknown/tool-health, cleanup | 4/3/3 | Fuzz fixtures, crash/timeout injection, no false clear/orphans |
| Event storm | Whole JSON grows/rewrites and UI truncates | Durable queue, dedup/suppression, partitions, pagination, quotas | 4/4/2 | All streams fail/recover repeatedly; bounded lag/storage |
| Worker spool/disk exhaustion | No spool today; future reports could be lost or host could fill | Byte/age quotas, disk reserve, priority eviction, health alarm, safe degradation | 4/3/2 | Fill disk/spool during broker outage; critical policy holds |
| Credential expiry/revocation | Current identity is unauthenticated; future rotation can stop fleet | Overlap rotation, explicit expiry health, emergency revocation, no fail-open | 5/2/3 | Expiry/revocation/rotation at fleet scale |
| Poison result/event | Current arbitrary state may break or grow projection | Strict schema, per-item rejection, bounded DLQ/quarantine, consumer isolation | 4/3/3 | Malformed deterministic replay; no partition stall |
| Partial deployment/schema skew | No version contract | Expand/migrate/contract, compatibility matrix, config version | 4/3/3 | Mixed N/N-1 control plane/workers and rollback |
| Zone/region loss | Single-host topology unavailable | Multi-zone control plane/DB/broker and spare worker capacity | 5/2/2 | Zone evacuation/game day |
| External endpoint abuse/SSRF | Worker fetches registered URL | Authorization, validation, redirect/DNS controls, egress firewall | 5/3/3 | SSRF suite including redirects and DNS rebinding |
| Noisy tenant | No capacity quota | Tenant token buckets, quotas, scheduler fairness | 4/3/3 | One tenant saturation while others meet SLO |
| Generated DASH origin overload | Master serves probe bytes | Dedicated HA origin/cache/object store | 4/3/2 | Segment fan-out/load and origin failover |

## 12. Phased migration roadmap

Each phase is independently deployable and reviewable. Preserve existing stream IDs, profiles, monitor IDs, and GUI-visible semantics unless an API version explicitly changes them. Named owners are roles to assign before execution, not evidence that those teams currently exist.

Phase dependencies are strict: Phase 1 requires the Phase 0 contract/measurements; Phase 2 requires Phase 1 identity/schema boundaries; Phase 3 requires Phase 2 durable lease/result semantics; Phase 4 requires Phase 3 ownership and drain behavior; each scale admission requires every preceding gate. A phase may prototype ahead but cannot carry production authority until dependencies pass.

Every phase must publish an immutable evidence bundle containing source/infrastructure/config/dataset versions, change and migration manifests, test reports, dashboards/SLO comparison, security findings, backup/restore evidence where applicable, record checksums, decision owner, and rollback record. Common rollback triggers are any security boundary breach, accepted stale-authority result, data loss above approved RPO, sustained critical freshness/SLO burn above the phase policy, or inability to restore the prior version within the approved window. The release owner declares rollback; the database owner controls data reversal; SRE controls traffic and worker drain. Phase 0 must replace these role labels and thresholds with named accountable owners and approved numbers.

### Phase 0 — product contract and measurement harness (**must have**)

Deliver:

- Approve stream/protocol/source mix, enabled checks, tier cadences, retention, tenant model, regions, SLOs, RPO/RTO, and cost ceiling.
- Instrument current probes for wall/CPU/bytes/RSS/FD/process outcome.
- Build deterministic healthy/slow/dead/malformed SRT and DASH fixtures and a synthetic control-plane/result load driver.
- Establish benchmark reports for single-check and mixed workload.

Gate: assumptions are signed off; benchmark is reproducible; evidence bundle includes workload manifest/schema and raw resource report; no 1,000-stream claim yet.

Current implementation publishes latest completed-batch CPU, worker/child peak
RSS, and Linux descriptor count through worker pressure. Per-check/media-byte
accounting, time-series retention, representative baselines, and the raw
resource report remain open.

Rollback: instrumentation flags off within one release operation; no data migration or accepted data loss.

### Phase 1 — secure and bound the existing slice (**must have before non-lab use**)

Deliver:

- Operator authentication/RBAC, worker mTLS identity, tenant/resource authorization.
- Strict versioned schemas, request/report size limits, timeouts, rate limits, idempotency IDs, SSRF/egress controls.
- Worker retry/backoff/jitter and explicit probe `unknown/stale` behavior.
- Health endpoints and foundational metrics/log correlation.

Gate: threat model reviewed; security/abuse tests pass; malformed/oversized/replayed requests cannot corrupt state; authentication failure and latency remain within approved policy.

Rollback: on auth outage or authorization defect, route only isolated lab traffic to the disabled-by-default legacy endpoint; never expose both publicly and never fail open. Revoke affected credentials and preserve audit records.

### Phase 2 — durable control-plane state (**must have**)

Deliver:

- Add production relational repository implementations for streams/config, workers, leases, results, alarms/events, and audit records.
- Use expand/migrate/contract schema process and dual-read comparison. Backfill SQLite registrations with stable IDs and alert profiles.
- Add idempotent report ingestion, config versions, lease epochs, indexed/paginated alarm/event APIs, retention, backup/PITR.

Compatibility: keep `FeedRegistrationStore`; export/import and checksum all records. During migration, SQLite remains rollback source until cutover verification, but only one system may accept writes at a time unless a transactional outbox guarantees dual-write consistency. Required artifacts include forward/reverse migration scripts, export/import and delta-replay tooling, pre/post row and semantic checksums, migration journal, and restore report.

Gate: migration and reversal rehearsal on a production-shaped copy, zero unexplained record/checksum differences, stale-result rejection, restore drill within approved RTO, and explicit data-loss result within RPO.

Rollback: trigger on checksum mismatch, migration error, stale-authority acceptance, or sustained DB SLO breach; stop writes, replay the journal/outbox delta to the prior store, run semantic checksums, then switch the feature flag. Phase 0 must approve the maximum rollback window; no contraction migration may begin until that window expires.

### Phase 3 — production worker runtime and scheduler (**must have**)

Deliver:

- Independent heartbeat, capability/capacity advertisement, durable leases and acknowledgements, fencing, stable cost-aware scheduling, drain/reconcile loops.
- Bounded concurrent execution with separate pools/tokens by cost, deadlines/cancellation, per-tenant fairness, local encrypted spool, and backpressure.
- Disable standalone global monitor once lease ownership is authoritative.

Gate: worker death/partition/duplicate/stale-report tests—including retained out-of-scope state and empty assignments—prove bounded failover and no duplicate authoritative monitoring; slow-stream storms preserve critical freshness.

Rollback: trigger on any accepted stale epoch, duplicate authority, unbounded queue/spool, or freshness breach; stop new leases, drain the canary worker pool, and temporarily return selected streams to the legacy monitor in trusted environments only after incrementing/fencing the new lease generation.

### Phase 4 — HA control plane and data-plane separation (**must have**)

Deliver:

- Replicated stateless APIs and consumers, leader-elected scheduler, HA DB/broker, multi-zone deployment and autoscaling.
- Generated-feed service separated from GUI/API; dedicated DASH origin/object store; explicit SRT allocation.
- Read model for fleet UI; filtered/paginated APIs or delta stream; cached/throttled previews.

Gate: API/scheduler/DB/broker/zone failover, rolling upgrade, restore, and game-day tests meet SLO/RPO/RTO, with infrastructure manifests and run outputs in the evidence bundle.

Rollback: trigger on failover/SLO/RPO/RTO breach; stop the rollout, drain new assignments, reverse traffic weights/DNS within the approved TTL, and use backward-compatible schemas. Retain the old data path only until the new path's soak and rollback rehearsal pass.

### Phase 5 — 1,000-stream admission (**must have**)

Deliver evidence from representative infrastructure:

- 24-hour steady-state and repeated failure storms at 1,000 streams.
- 30% capacity headroom after loss of one failure domain.
- Detection, freshness, reassignment, alarm consistency, queue/spool, and resource SLOs all pass.
- On-call runbooks, dashboards, capacity alarms, restore evidence, security sign-off.

Do not advance on averages alone; enforce p95/p99 and zero stale-authority violations.

### Phase 6 — 5,000 and 10,000 gates

Repeat full capacity/failure/DR/security evidence at each target. Tune partitioning, broker/database capacity, worker locality, and UI read models based on measurements. Admission requires all Section 13.3 criteria at the target plus 30% check-rate headroom after one failure-domain loss. Roll back target traffic to the last admitted scale on any stale-authority acceptance, data loss beyond RPO, p99 freshness/failover breach for two consecutive evaluation windows, unbounded queue/spool growth, or resource saturation above the approved safe ceiling. Linear extrapolation is forbidden.

### Later optimization (not production blockers if gates pass)

- Multi-region active/active scheduling.
- Shared regional ingest/decode fan-out.
- Predictive autoscaling.
- Long-term analytics/data lake.
- Seamless protocol-specific probe reuse.

## 13. Verification plan and acceptance criteria

### 13.1 Test layers

| Layer | Required proof |
|---|---|
| Unit | Lease transition/CAS, scheduler cost/fairness, result dedup/order, config compatibility, alarm unknown/stale semantics, quotas |
| Contract | Versioned operator/worker schemas, N/N-1 compatibility, size limits, auth claims, error/retry semantics |
| Integration | Real DB/broker/object store, transactional outbox/inbox, migration, worker spool/replay, SRT/DASH fixtures |
| Security | AuthN/AuthZ/tenant isolation, mTLS rotation/revocation, SSRF/redirect/DNS rebinding, replay, oversized/decompression payloads, rate limits |
| Load | 1k/5k/10k mixed streams, UI readers, result bursts, stable and failure-storm workloads; saturation curve |
| Soak | At least 24 hours per admission target with periodic failures, restarts, and leak/orphan checks |
| Chaos/failover | Kill/partition control plane, scheduler, worker, DB, broker, origin, and zone; inject latency/packet loss/disk pressure |
| DR | Backup/PITR restore, global generation fencing, measured RPO/RTO |
| Upgrade | Mixed-version rolling deploy, schema expand/contract, worker drain, rollback at each phase |
| Human-visible | Operator can find/filter/acknowledge an alarm, see stale/unknown worker state, inspect assignment history, and follow runbook |

### 13.2 Required harnesses/commands to add

Exact names are proposals and must be implemented before use:

```text
python -m videosim fixture-fleet --manifest <scenario.yaml>
python -m videosim worker-benchmark --scenario <scenario.yaml> --json <report.json>
python -m videosim control-plane-load --streams 1000 --workers <n> --duration 24h
python -m videosim verify-assignments --require-single-authority
python -m videosim verify-alarm-consistency --results <capture>
python -m videosim chaos --scenario worker-partition|db-failover|event-storm
python -m videosim capacity-check --report <report.json> --policy <gate.json>
```

`capacity-check` is now implemented with JSON evidence/policy inputs and the
versioned F5 policy at `scale/policies/f5-1000.json`. The remaining proposed
harnesses and the production-like evidence they must generate are not
implemented; verifier availability is not scale admission.

CI should run unit/contract/security fixture tests. Representative load, soak, failover, and DR run in a production-like scheduled environment and publish immutable reports tied to code, config, infrastructure version, and dataset.

A workload manifest must include schema version, random seed, target scale, SRT/DASH and generated/external percentages, region/zone placement, check profiles and cadences, endpoint health/latency/malformed distributions, event-storm schedule, worker shape/count/concurrency, infrastructure versions, duration, and expected invariants. The immutable evidence manifest must include source commit, dirty-state declaration, container/image digests, infrastructure/config/workload hashes, start/end timestamps, command and exit status, raw artifact locations and SHA-256 hashes, summarized p50/p95/p99 metrics, acceptance-policy version, pass/fail per criterion, skipped checks with reasons, and approving owner. A verifier must fail closed when required fields or artifacts are absent.

### 13.3 Admission criteria

For each scale target:

1. **Assignment correctness:** every desired monitored stream has exactly one current authoritative lease, or an explicitly measured transition grace; no conflicting epoch is accepted.
2. **Bounded failover:** after worker death/partition, 95% of affected streams regain authority within 45 seconds and 99% within 90 seconds (unless approved SLO differs).
3. **No duplicate authority:** reordered, duplicated, replayed, and stale reports never change current state; reports containing retained items outside current assignment scope—including empty assignments—are rejected per item and cannot create duplicates; diagnostic late-data storage is separate.
4. **Alarm consistency:** result-to-current-state and alarm projections reconcile to source events with zero unexplained false clear; probe failure yields unknown/stale.
5. **Backpressure:** at saturation, queues/spools remain bounded, lower-priority checks defer first, critical freshness meets SLO, and recovery completes without a second storm.
6. **Sustained load:** 24 hours at target plus 30% traffic/check-rate headroom, with one worker failure domain unavailable, while meeting p95/p99 latency and resource limits.
7. **Security:** no cross-tenant read/write/report, unauthorized worker, replay, oversized request, or SSRF test succeeds.
8. **Durability:** DB/broker failover and restore meet approved RPO/RTO; leases from an old generation cannot mutate restored state.
9. **Operability:** dashboards and alerts detect injected failures; an operator completes runbook actions and audit evidence is retained.
10. **Rollback:** current release and schema can be rolled back within the documented window without losing accepted configuration/alarm events.

## 14. Must-have summary

Before production at 1,000 streams:

- Approved workload/SLO/retention/tenant assumptions and measured worker capacity.
- Authenticated/authorized, bounded APIs and hardened external endpoint handling.
- Shared production database; durable idempotent ingestion; queryable retained alarms/events.
- Durable leases with epochs, independent heartbeats, fencing, stable capacity-aware scheduling, and reconciliation.
- Bounded concurrent worker execution, cancellation, retry/backoff, backpressure, fairness, and spool.
- Explicit healthy/unhealthy/unknown/stale semantics and probe self-health.
- Replicated multi-zone control plane, HA storage/broker, tested backup/restore/failover.
- Generated media data plane separated from control plane.
- Production telemetry, SLOs, runbooks, security review, and 24-hour representative admission evidence.

Passing the existing unit tests is valuable regression evidence for MVP behavior but proves none of the above scale claims.

## 15. Completion audit, evidence manifest, blocked claims, and next inputs

### Audit evidence manifest

| Field | Evidence |
|---|---|
| Audit date | 2026-07-11 |
| Implementation baseline | `df8e8f235a72b643f130c3164420a9a83508ad17` |
| Working-tree scope | Audited committed implementation plus audit-related edits to this file, `README.md`, `docs/distributed-architecture.md`, `KNOWN_LIMITATIONS.md`, and `docs/work-log.md`; excluded unrelated pre-existing `.DS_Store`, `.pi-subagents/`, and `design_docs/` |
| Repository inspection | Complete reads/targeted line inspection of `videosim/gui.py`, `worker.py`, `monitor.py`, `feed_store.py`, `validator.py`, frontend polling, Compose, distributed/implementation/monitoring/stability docs, limitations/gaps, and relevant tests |
| Independent review | Fresh-context trace and production review, followed by a requirement-by-requirement review of this audit; material findings were reconciled into the document |
| Static validation | Markdown link/path check, requirement keyword/checklist audit, citation spot-check, `git diff --check`, and task-scope diff inspection; exact final results recorded in `docs/work-log.md` |
| Regression validation | Documentation contract, full Python unit suite, UI build/audit, and Compose config checks; exact final results or skipped reasons recorded in `docs/work-log.md` |
| Scale validation | Not run: no implemented target architecture, representative workload, or production-like 1k/5k/10k environment |
| Failover/security/DR validation | Not run: these are roadmap acceptance gates, not current capabilities |
| Review artifact | This audit and the required task-only finalization commit are the reviewable record; transient subagent scratch output is excluded. The resulting commit ID is reported in the final task response because a commit cannot self-reference its own ID. |

### Confirmed-claim attestation ledger

| Claim | Status | Source/evidence | Limitation |
|---|---|---|---|
| Control plane and generated-feed lifecycle are process-local | Confirmed | `videosim/gui.py:242-267, 458-523, 637-665, 1620-1632` | Static inspection, not failover test |
| Feed registration alone persists in SQLite | Confirmed | `videosim/feed_store.py:11-150`; `videosim/gui.py:296-433` | No multi-process DB test |
| Worker assignment is in-memory 60-second TTL plus modulo split | Confirmed | `videosim/gui.py:52-53, 1340-1380` | Basic unit behavior only |
| Worker probing is serial and subprocess-heavy | Confirmed | `videosim/worker.py:34-58`; `videosim/monitor.py:203-218, 333-373` | Per-check resource cost unmeasured |
| Report ingestion lacks epoch/idempotency/scope enforcement | Confirmed | `videosim/worker.py:22-31, 43-55`; `videosim/gui.py:1565-1582` | Static defect trace; no production race test |
| Rebalance can re-append retained out-of-scope state | Confirmed | Worker retains state at `videosim/worker.py:43-55`; ingestion appends all arrays at `videosim/gui.py:1565-1577` | Reproduction test is a required migration gate, not added by this audit-only task |
| Browser performs full-state and preview polling | Confirmed | `frontend/src/main.jsx:67-115`; state/preview paths at `videosim/gui.py:1282-1449` | Browser concurrency unmeasured |
| Compose can enable overlapping monitor ownership paths | Confirmed | `docker-compose.yml:15-37` | Actual lost update not load-tested |
| Existing tests do not prove thousand-stream production behavior | Confirmed | `tests/test_worker.py`, `tests/test_gui.py`, `tests/test_monitor.py`, `tests/test_feed_store.py`; no scale harness found | Full regression passing remains only MVP evidence |
| 1k/5k/10k capacity | Unverified | No repository benchmark or representative environment | Section 7 values are models only |

### Confirmed findings

- The current first slice's control plane, worker registry, assignment, report merge, monitor state, and generated-feed lifecycle have been traced to code.
- Current persistence, API trust, serial probing, browser polling, deployment, and test boundaries have been identified.
- A target architecture, capacity formula, scenario demand model, security/HA/operations design, protocol distinctions, FMEA, phased migration, rollback approach, and measurable verification gates are documented.

### Modeled/proxy evidence

- Scenario check rates and report ingress are arithmetic based on Section 7 assumptions.
- Worker-count sensitivity is illustrative only.
- Proposed SLOs, RPO/RTO, event rates, payload sizes, and 60% utilization require stakeholder approval and measurement.

### Blocked/unverified claims

This audit does **not** claim that VideoSim currently supports 1,000, 5,000, or 10,000 monitored streams. Measured certification is blocked by:

- No approved stream/check/cadence/region/tenant workload profile.
- No production-like compute, network, database, broker, object store, or multi-zone environment.
- No implemented production architecture or load/failure harness.
- No per-protocol/check resource benchmarks.
- No business-approved SLO, retention, RPO/RTO, or cost target.
- Pending long-run MVP soak evidence already recorded in `docs/stability-report.md` and `TEST_GAPS.md`.

### Inputs needed next

1. Product/SRE decisions for workload mix, detection cadence, availability, freshness, retention, tenancy, regions, RPO/RTO, and cost.
2. Representative reachable SRT/DASH fixtures and permission to generate failure traffic.
3. Target orchestration/cloud platform and approved managed database/broker choices or constraints.
4. A production-like benchmark environment and security identity/network model.
5. Agreement to begin Phase 0 measurement without presenting modeled figures as capacity evidence.
