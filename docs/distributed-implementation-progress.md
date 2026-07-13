# Distributed Architecture Implementation Progress

**Objective:** implement the production target described in
[`production-readiness-audit.md`](production-readiness-audit.md) without
regressing the existing SRT/DASH simulator.

This is the active implementation ledger. A green unit suite is necessary but
is not sufficient evidence for a scale or production-readiness claim.

## Completion contract

The objective is complete only when all rows below are implemented and the
linked evidence passes in a production-like environment.

| Gate | Required artifacts | Required evidence | Status |
|---|---|---|---|
| F0 — contract and measurement foundation | Versioned worker contract; assignment generation/token; strict report ownership; worker-state pruning; independent heartbeat; probe instrumentation; deterministic control-plane benchmark | Rebalance, empty-assignment, stale-instance/generation/token, malformed/out-of-scope report, heartbeat-over-TTL, persistence failure, and concurrency tests; benchmark invariant report | Partial: local contracts/control-plane benchmark, real-probe worker benchmark, DASH/SRT healthy/slow/dead/malformed fixtures, shared-endpoint mixed-1,000 composer, batch CPU/peak-RSS/FD instrumentation, and scale-evidence verifier implemented; representative independent workload/resource report missing |
| F1 — secure bounded APIs | Operator and worker authentication/authorization; mTLS workload identity; request schemas/limits; rate limits; SSRF controls; audit log; retry policy | Contract, abuse, replay, tenant-isolation, credential rotation, and SSRF tests | Core Compose/VM boundary implemented; external OIDC/rotation/tenant evidence pending |
| F2 — durable control plane | Production repository adapters; migrations; workers, leases, assignments, results, alarms/events, audit records; idempotent ingestion; backup/restore | Migration/reversal checksums, fencing, stale-result rejection, idempotency, PITR/RPO/RTO evidence | In progress: PostgreSQL/JetStream foundation, HTTP worker-v2, direct PostgreSQL monitor-alarm/operator-read projection, selective durable HTTP audit, and inbox-deduplicated replay ledger implemented with local service/restore evidence; event-driven read authority and production PITR/RPO/RTO remain |
| F3 — scalable workers | Independent heartbeat; bounded protocol-aware concurrency; deadlines/cancellation; capacity tokens; backpressure; encrypted spool; drain | Worker death/partition/ABA, slow-stream storm, spool recovery, fairness, and freshness evidence | Partial: durable total/SRT/DASH admission, spool-blocked assignment exclusion with explicit shortfall, separate validation/deep concurrency tokens, phase-window probe submission, rotating aggregate-budget validation deferral, validation-first ordering, staggered deep-check cadence, per-stream deadlines, bounded latest cycle/deferral/spool pressure snapshots, bounded encrypted write-ahead report spooling, hard bounds for built-in media subprocess/poll waits, final-report-ordered fenced drain, and same-process recovery from a local whole-domain assignment transport partition are implemented; weighted check-cost/tenant fairness, pressure hysteresis/history/alerts/recovery SLOs, durable fleet-queue backpressure, multi-host spool chaos/key rotation, and production evidence remain open |
| F4 — HA and data-plane split | Replicated stateless APIs; leader-elected scheduler; HA DB/broker; generated-feed service; dedicated DASH origin; SRT allocation; observability | API/scheduler/storage/zone failover, rolling upgrade/rollback, game-day, and operator runbook evidence | Partial: two APIs use current PostgreSQL catalog/config authority; generated desired-state mutations route across replicas; a separate runtime holds feed/port/version-ready locks; generated SRT allocation is transaction-serialized and unique-indexed across an exact 1,000-port range; detail reads expose current lock-backed owner health; and the local two-API kill/recreate workflow revalidated real media. Persistent leadership, HA control/storage, multi-host SRT routing, HA DASH origin, independent media headroom, and production failover remain open |
| F5 — 1,000-stream admission | Production-like workload manifest and immutable evidence bundle | 24-hour run, 30% headroom after one failure-domain loss, all audit Section 13.3 criteria | Policy/verifier, exact 33-worker/three-domain candidate, scheduler-loss model, worker-domain Compose, and three-shard fixture-domain Compose/startup contracts implemented; two API processes started after 1,320 external rows already existed with zero cached feeds, then returned all rows in 14 bounded pages and committed two unique concurrent creates; local repeated hard loss recovered in 65.466/66.018 seconds and a local same-process partition recovered in 61.194 seconds with only 440 required moves and zero healthy authority changes. Calibrated media freshness failed and the candidate remains unapproved/unrun on three hosts |
| F6 — 5,000-stream admission | Independently sized infrastructure and workload evidence | Full load/soak/failure/security/DR gate at 5,000; no linear extrapolation | Not started |
| F7 — 10,000-stream admission | Independently sized infrastructure and workload evidence | Full load/soak/failure/security/DR gate at 10,000; no linear extrapolation | Not started |

## Foundation prompt-to-artifact checklist

| Requirement | Planned artifact/evidence | Status |
|---|---|---|
| Schema compatibility | `videosim/control_plane.py` contract constants and validation | Implemented; strict by default |
| Restart and ABA protection | Per-process instance ID plus monotonic assignment generation | Implemented; process-local transition fence |
| Authoritative assignment scope | Server-derived stream ownership; caller scope never grants authority | Implemented and tested |
| Stale report fencing | Instance, generation, and opaque assignment token validation before mutation | Implemented with HTTP 409 tests |
| Out-of-scope containment | Validate every alarm/event/pending/catalog-observation/probe-metric item; reject malformed and report dropped IDs | Implemented with HTTP 422 and direct tests |
| Empty/rebalanced worker safety | Prune retained monitor state and recompute metric aggregates before and after every monitor pass | Implemented and tested |
| Long-batch liveness | Independent heartbeat remains active while serial probes run | Implemented with batch-over-TTL regression |
| Conflict recovery | HTTP 409 causes bounded immediate refetch instead of worker termination | Implemented and tested |
| Persistence honesty | Failed monitor-state write returns retryable HTTP 503 rather than success | Implemented and tested |
| Lifecycle concurrency | Assignment reads, stream allocation, start, and delete use one synchronization boundary | Implemented with deterministic concurrency tests |
| Capacity observability | Monotonic per-check and batch duration/outcome summaries with bounded cardinality | Implemented and scoped at ingestion |
| Worker pressure snapshot | Independent heartbeat publishes bounded assigned/cycle/deferral/batch/spool fields; PostgreSQL operator state exposes the latest snapshot | Implemented and schema-tested; history, alerts, recovery SLO, and saturation evidence remain open |
| Pressure-aware stable placement | Durable scheduler gives spool-blocked workers no assignments, retains schedulable fresh owners within capacity during incomplete recovery, reports unmet demand, and limits modeled loss to unavailable ownership | Implemented with modeled and local 33-container/PostgreSQL loss/rejoin/second-loss coverage plus a same-process whole-domain partition: each failed domain's 440 owners moved, all 880 healthy authority tuples stayed, and partition recovery completed in 61.194 seconds; separate-host timing and media/headroom certification remain open |
| Process-local failure benchmark | Removes configured workers after warmup, requires complete unique survivor ownership, rejects stale failed-worker reports, and reports minimum/excess assignment moves | Implemented and 1,300-stream-tested; current local round robin produced 1,044 excess moves, and this harness does not measure durable TTL, network, or failure-domain behavior |
| Scale evidence integrity | F5 policy/verifier requires all ten admission criteria, workload duration/headroom/failure-domain survivor tokens, clean successful runs, relative hashed artifacts, and no skipped checks | Implemented and fail-closed tested; checked-in candidate retains exact 1,320/660/660 total/SRT/DASH tokens after one domain loss, but no production-like evidence bundle exists |
| Worker resource snapshot | Existing heartbeat pressure includes completed-batch worker+child CPU, worker/child peak RSS, and Linux post-batch descriptor count | Implemented and schema-tested; no history, media-byte accounting, baseline, or capacity curve |
| Worker resource benchmark | Hashed exported-state scenario drives real monitor probes and reports cycle/CPU percentiles, aggregate/per-protocol outcomes, unique validation-start coverage, conservative wall-time freshness bounds, peak RSS, and descriptors | Implemented and protocol-calibrated; independent-source scenario, saturation curve, and fleet evidence missing |
| DASH/SRT endpoint fixture matrices | Existing generators plus HTTP/GStreamer fault services provide healthy, 30-second slow, dead, and malformed endpoints with bounded per-behavior URL counts; scaled SRT listeners share one encoded multicast source | Simultaneous 500+500 smoke passed; exact 220+220 source-domain manifests and sampled startup validation implemented; 660+660 single-host saturation showed freshness failure and SRT memory growth; three-host load, per-stream independence, reconnect-storm, cross-product, and long-run evidence remain |
| Mixed fixture scenario | Seeded exact allocation composes protocol/behavior mixes from one or more hashed fixture states, rejects duplicate shard endpoints, and reports state counts plus actual endpoint sharing | Three simulated source shards compose exact 1,320/660/660 distinct URLs; the earlier live 1,320 input failed saturation, and no multi-host generation or capacity evidence exists |
| Validation rotation cadence | Opt-in benchmark gates require every running assignment to start validation, start at least twice, and remain within bounded initial/repeat/trailing cycle and conservative wall-time gaps without exporting ID sets in worker reports | Implemented and 1,000-URL/250-cycle Linux-tested with 125-cycle and 89.651-second maximum upper bounds; healthy URLs still share sources, and the gate is not an approved SLO or capacity evidence |
| Capacity admission | Durable workers advertise `capacity.maxStreams`; scheduler reports over-capacity shortfall without over-admitting | Implemented and unit-tested; no media capacity certification |
| Protocol admission | `capacity.maxSrtStreams` and `capacity.maxDashStreams` combine with total capacity and leave excess protocol demand unassigned | Implemented and mixed 1,000-stream invariant-tested; static operator counts, not weighted-cost certification |
| Batched lease lifecycle | Worker assignment offer/renew and acknowledgement arrays each use one ordered atomic PostgreSQL transaction while retaining single-lease wrappers | Implemented with one worker-fence query plus one set-based lease statement and a 1,000-row integration check; multi-controller leadership and production DB saturation remain open |
| Batched probe-report projection | Report ingestion bulk-reads feed, lease, result-ID, and current-state fences, then batch-writes current state, lease sequences, and event retention while retaining immutable result inserts and alarm transitions | Implemented with a 60-stream/120-result query-bound integration check; local 1,320-stream fault follow-up removed HTTP 499/5xx, while sustained production saturation and lock telemetry remain open |
| Scheduler transaction leadership | Durable external-feed catalog, membership/owner reads, offer/renew, and revocation share one tenant-scoped PostgreSQL advisory-lock transaction | Implemented with catalog-row locking, queued two-store connection-loss takeover, and a local two-process 1,320-feed smoke with zero contention retries; persistent leader, replicated deployment, lock-wait telemetry, and failover SLO remain open |
| Operator feed read model | Viewer-authorized versioned API reads the durable feed catalog with a bounded keyset cursor instead of a process startup cache | Implemented with 100-row React/root bootstrap, 200-row API maximum, lightweight overview, one-feed scoped detail, fail-closed row validation, SQLite fallback, and generated owner/session health read directly from version-ready PostgreSQL locks; snapshot cursors, restart history, and HA remain open |
| Durable API restart/create | PostgreSQL API process state remains empty across restart and create IDs do not depend on replica-local catalog state | Implemented with no durable preload, UUID-backed IDs, expected-version collision rejection, atomic generated-SRT port allocation, exact 1,000-port concurrent coverage, and two-replica live create evidence; idempotent create/retry and multi-host endpoint allocation remain open |
| Replica-safe durable mutations | Durable forms require a positive client config version and PostgreSQL performs atomic compare-and-swap plus success audit from any API replica | Implemented for external update/alert/delete and generated update/start/fault/stop/delete with HTTP 400/409 contracts; generated media reconciles outside API processes. Idempotent create remains open |
| Generated runtime ownership | A capacity-bounded media runtime holds feed and SRT-port session locks, then takes a config-version ready lock only after child launch | Implemented with two-runtime takeover/config-restart/stop integration and two-API kill/recreate API/media/log evidence; restart history, multi-host routing, HA origin, and media capacity admission remain open |
| Bounded execution | `--max-concurrent-checks` and `--max-concurrent-deep-checks` independently limit validation/deep streams and executor submissions to one phase-sized window while preserving per-stream check order | Implemented and barrier-tested; static tokens on one executor, no media-capacity certification |
| Slow-stream containment | `--stream-budget-seconds` reports exhausted streams as timeout, caps built-in media subprocess/poll waits, and defers remaining checks without clearing prior alarms | Implemented and unit-tested across GStreamer/FFmpeg/FFprobe/DASH waits; slow-stream-storm evidence remains open |
| Critical-first scheduling | One bounded pool completes admitted validation windows before starting TR-101/frame-rate/loudness work; validation result and deadline carry across phases | Implemented and phase-order tested; black/frozen cost and weighted check-cost/tenant fairness remain open |
| Deep-check cadence | `--deep-check-interval-seconds` spreads TR-101/frame-rate/loudness work over stable per-stream offsets; deferral stays inconclusive | Implemented and concurrent-cadence tested; operator configured, not dynamic saturation feedback |
| Batch pressure guard | `--batch-budget-seconds` stops new validation windows after aggregate budget exhaustion, rotates skipped streams into the next cycle, preserves alarms, and defers deep work | Implemented and pressure-tested; bounded local queue only, not a durable fleet queue, hard wall deadline, or capacity certification |
| Encrypted report spool | Worker v2 encrypts and fsyncs exact fenced payloads before send, replays before new registration, deletes accepted/stale-fenced entries, enforces a byte quota, and pauses probes while blocked | Implemented and unit-tested; production outage/restart/disk-fill, repair, key rotation, and fleet telemetry evidence remain open |
| Graceful drain | SIGINT/SIGTERM completes the current report, stops heartbeats, and marks the exact durable worker incarnation plus live leases draining before higher-epoch reassignment | Implemented and unit/HTTP-tested; PostgreSQL integration test is implemented but not run in this environment, and hard-kill recovery still uses lease expiry |
| Startup evidence | Isolated Compose app/worker boot followed by API create/start/normal validation/stop, with process state and Docker logs retained | Local workflow implemented and repeatedly run; three worker-domain projects were booted locally and checked through mTLS API/logs, while independent-host and authenticated production Chrome/OIDC evidence remains open |
| Baseline load evidence | In-process control-plane benchmark explicitly labeled non-media/non-capacity | Implemented; local 1,000-stream invariant run recorded in work log |
| Regression safety | Targeted tests, full unit suite, UI build/audit, Compose config, docs checks | Implemented; final commands recorded in work log |
| Reviewability | `docs/work-log.md` entry and task-only commit | Implemented in the F0 foundation commit |

## Security boundary checklist

| Requirement | Artifact/evidence | Status |
|---|---|---|
| Deployment decision | Docker Compose/VM target | Selected |
| Worker identity | Nginx worker CA verification, certificate-CN header, app identity match | Implemented; local live mTLS smoke |
| Operator identity | oauth2-proxy OIDC and app viewer/admin group enforcement | Implemented configuration/unit HTTP tests; real IdP pending |
| Header spoof resistance | Internal-only app plus 32+ character proxy secret | Implemented |
| Transport | TLS server endpoint and worker CA/cert/key CLI support | Implemented |
| Input bounds | Nginx/app 1 MiB limit and report collection/identifier caps | Implemented |
| Destination policy | Inline credential rejection, DNS/IP classification, explicit private/suffix policy | Implemented; VM egress and redirect-aware policy pending |
| Rate limits | Separate Nginx operator and worker zones | Implemented configuration; production tuning pending |
| Retry policy | Bounded exponential jitter for transient assignment/report failures; 409 refetch separate | Implemented |
| Auditability | Stdout authorization events plus selective immutable PostgreSQL denial/mutation audit, transactional outbox, and non-owner service roles | Implemented for selected scope; allowed reads/workers/local runtime actions and external/WORM archive remain open |
| Credential lifecycle | Managed CA/secret rotation and revocation | Not implemented; F2/F4 gate |
| Tenant isolation | Durable tenant/resource grants | Not implemented; F2 gate |

## Durable control-plane checklist

| Requirement | Artifact/evidence | Status |
|---|---|---|
| Durable technology decision | PostgreSQL 17 authority; NATS 2.11 JetStream committed-event transport | Selected and Compose-provisioned |
| Versioned schema | `migrations/001_durable_control_plane.sql` plus destructive test-only down migration | Implemented |
| Migration safety | Advisory lock, applied checksum/name validation, unknown-version rejection | Implemented and integration-tested |
| Feed cutover | PostgreSQL adapter and idempotent `import-sqlite-feeds` | Implemented and integration-tested |
| Worker incarnation and lease epochs | DB-time heartbeat/expiry, offered→acknowledged active leases, sticky balanced placement, monotonic epochs with per-epoch sequence reset | PostgreSQL-backed HTTP worker v2 implemented; SQLite v1 retained only for lab compatibility; real HTTP integration-tested |
| Result ingestion | Immutable report/result IDs and hashes; worker/lease/config/per-lease-sequence/observation-time fencing; stored duplicate dispositions | HTTP v2 durably ingests scoped `probe.*` and catalog monitor observations; duplicate/stale/reassigned reports cannot rerun direct projection |
| Alarm authority | Transactional current check/alarm/pending projection, immutable edges, only explicit healthy clears; inconclusive or omitted observations preserve; server-owned delay/repeat/retention | PostgreSQL direct `/state.json` reads implemented and integration-tested; inbox-deduplicated consumer replay remains open |
| Durable audit | Append-only audit rows, immutable audit outbox envelopes, selective HTTP denial/mutation wiring, and non-owner runtime roles | Implemented and integration-tested; denial persistence is best-effort and external/WORM archive coverage remains open |
| Event transport | Hash-immutable transactional outbox, concurrent `SKIP LOCKED`, retry/dead state, exact JetStream policy, at-least-once IDs, consumer-inbox schema | Producer implemented and local integration-tested; no consumer deployed, so consumer/read cutover remains blocked |
| Backup/restore | Custom-format scripts, migration check, informational generation increment, enforced lease expiry, explicit broker mode, repeatable-read semantic comparator | Implemented; local empty/retained-broker functional restores passed; PITR/RPO/RTO not certified |
| Deployment | Production Compose role-init → migrate → role-grants → app/publisher/pruner dependencies and persistent volumes | Implemented/config validated; PostgreSQL/NATS remain single-instance |
| Scale evidence | Production-like durability/load/failover data | Not started; no scale claim |

The detailed authority model, exact commands, local evidence boundary, rollback,
and remaining cutover work are in
[`durable-control-plane.md`](durable-control-plane.md).

## Constraints and deferred decisions

- F0 itself added no production database, broker, orchestrator, or authentication system. F2 now adds PostgreSQL/JetStream, durable PostgreSQL worker v2, and a direct PostgreSQL operator projection; SQLite v1 retains its isolated JSON view.
- The v1 in-process generation remains a trusted-lab transition fence, not an
  HA lease. Production-path v2 uses durable epochs and transactional fencing;
  transaction-scoped PostgreSQL leadership now serializes assignment decisions,
  while replicated deployment and persistent HA scheduling remain an F4 gate.
- Legacy reports may be accepted only in an explicit compatibility mode, still
  constrained to current server-derived scope and visibly marked as legacy.
- Control-plane benchmark numbers are not media-monitoring capacity and cannot
  satisfy F5-F7.
- PostgreSQL and NATS JetStream are selected for F2. HA topology and the F4
  orchestration decision remain open and require platform evidence.

## Blocked completion inputs

Full completion ultimately requires approved workload/SLO/retention/tenant and
RPO/RTO contracts, representative SRT/DASH fixtures, production identity and
network policy, a target orchestration platform, approved durable services, and
production-like infrastructure for 1,000/5,000/10,000-stream gates.
