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
| F0 — contract and measurement foundation | Versioned worker contract; assignment generation/token; strict report ownership; worker-state pruning; independent heartbeat; probe instrumentation; deterministic control-plane benchmark | Rebalance, empty-assignment, stale-instance/generation/token, malformed/out-of-scope report, heartbeat-over-TTL, persistence failure, and concurrency tests; benchmark invariant report | Implemented and locally verified; not a scale gate |
| F1 — secure bounded APIs | Operator and worker authentication/authorization; mTLS workload identity; request schemas/limits; rate limits; SSRF controls; audit log; retry policy | Contract, abuse, replay, tenant-isolation, credential rotation, and SSRF tests | Core Compose/VM boundary implemented; external OIDC/rotation/tenant evidence pending |
| F2 — durable control plane | Production repository adapters; migrations; workers, leases, assignments, results, alarms/events, audit records; idempotent ingestion; backup/restore | Migration/reversal checksums, fencing, stale-result rejection, idempotency, PITR/RPO/RTO evidence | In progress: PostgreSQL/JetStream foundation plus HTTP worker-v2 lease/probe-result shadow cutover and local service/restore evidence implemented; monitor-alarm reads, security audit, consumers, and production PITR/RPO/RTO remain |
| F3 — scalable workers | Independent heartbeat; bounded protocol-aware concurrency; deadlines/cancellation; capacity tokens; backpressure; encrypted spool; drain | Worker death/partition/ABA, slow-stream storm, spool recovery, fairness, and freshness evidence | Not started |
| F4 — HA and data-plane split | Replicated stateless APIs; leader-elected scheduler; HA DB/broker; generated-feed service; dedicated DASH origin; SRT allocation; observability | API/scheduler/storage/zone failover, rolling upgrade/rollback, game-day, and operator runbook evidence | Not started |
| F5 — 1,000-stream admission | Production-like workload manifest and immutable evidence bundle | 24-hour run, 30% headroom after one failure-domain loss, all audit Section 13.3 criteria | Not started |
| F6 — 5,000-stream admission | Independently sized infrastructure and workload evidence | Full load/soak/failure/security/DR gate at 5,000; no linear extrapolation | Not started |
| F7 — 10,000-stream admission | Independently sized infrastructure and workload evidence | Full load/soak/failure/security/DR gate at 10,000; no linear extrapolation | Not started |

## Foundation prompt-to-artifact checklist

| Requirement | Planned artifact/evidence | Status |
|---|---|---|
| Schema compatibility | `videosim/control_plane.py` contract constants and validation | Implemented; strict by default |
| Restart and ABA protection | Per-process instance ID plus monotonic assignment generation | Implemented; process-local transition fence |
| Authoritative assignment scope | Server-derived stream ownership; caller scope never grants authority | Implemented and tested |
| Stale report fencing | Instance, generation, and opaque assignment token validation before mutation | Implemented with HTTP 409 tests |
| Out-of-scope containment | Validate every alarm/event/pending/probe-metric item; reject malformed and report dropped IDs | Implemented with HTTP 422 and direct tests |
| Empty/rebalanced worker safety | Prune retained monitor state and recompute metric aggregates before and after every monitor pass | Implemented and tested |
| Long-batch liveness | Independent heartbeat remains active while serial probes run | Implemented with batch-over-TTL regression |
| Conflict recovery | HTTP 409 causes bounded immediate refetch instead of worker termination | Implemented and tested |
| Persistence honesty | Failed monitor-state write returns retryable HTTP 503 rather than success | Implemented and tested |
| Lifecycle concurrency | Assignment reads, stream allocation, start, and delete use one synchronization boundary | Implemented with deterministic concurrency tests |
| Capacity observability | Monotonic per-check and batch duration/outcome summaries with bounded cardinality | Implemented and scoped at ingestion |
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
| Auditability | Structured authorization events to stdout | Implemented; durable immutable store pending F2 |
| Credential lifecycle | Managed CA/secret rotation and revocation | Not implemented; F2/F4 gate |
| Tenant isolation | Durable tenant/resource grants | Not implemented; F2 gate |

## Durable control-plane checklist

| Requirement | Artifact/evidence | Status |
|---|---|---|
| Durable technology decision | PostgreSQL 17 authority; NATS 2.11 JetStream committed-event transport | Selected and Compose-provisioned |
| Versioned schema | `migrations/001_durable_control_plane.sql` plus destructive test-only down migration | Implemented |
| Migration safety | Advisory lock, applied checksum/name validation, unknown-version rejection | Implemented and integration-tested |
| Feed cutover | PostgreSQL adapter and idempotent `import-sqlite-feeds` | Implemented and integration-tested |
| Worker incarnation and lease epochs | DB-time heartbeat/expiry, offered→acknowledged active leases, monotonic epochs with per-epoch sequence reset | PostgreSQL-backed HTTP worker v2 implemented; SQLite v1 retained only for lab compatibility; real HTTP integration-tested |
| Result ingestion | Immutable report/result IDs and hashes; worker/lease/config/per-lease-sequence/observation-time fencing; stored duplicate dispositions | HTTP v2 durably ingests scoped `probe.*` checks before a durable-fenced retryable JSON shadow; real worker/retry/rebalance integration-tested |
| Alarm authority | Transactional current check/alarm projection, immutable edges, only explicit healthy clears; inconclusive states preserve | Repository primitive implemented and integration-tested; operator reads remain JSON-backed |
| Durable audit | Immutable audit IDs plus transactional outbox | Repository primitive implemented; security stdout path not connected |
| Event transport | Hash-immutable transactional outbox, concurrent `SKIP LOCKED`, retry/dead state, exact JetStream policy, at-least-once IDs, consumer-inbox schema | Producer implemented and local integration-tested; no consumer deployed, so consumer/read cutover remains blocked |
| Backup/restore | Custom-format scripts, migration check, informational generation increment, enforced lease expiry, explicit broker mode, repeatable-read semantic comparator | Implemented; local empty/retained-broker functional restores passed; PITR/RPO/RTO not certified |
| Deployment | Production Compose migration/init/app/publisher dependencies and persistent volumes | Implemented/config validated; PostgreSQL/NATS remain single-instance |
| Scale evidence | Production-like durability/load/failover data | Not started; no scale claim |

The detailed authority model, exact commands, local evidence boundary, rollback,
and remaining cutover work are in
[`durable-control-plane.md`](durable-control-plane.md).

## Constraints and deferred decisions

- F0 itself added no production database, broker, orchestrator, or authentication system. F2 now adds PostgreSQL/JetStream and uses durable epochs for PostgreSQL-backed worker v2, while the JSON operator projection remains transitional.
- The v1 in-process generation remains a trusted-lab transition fence, not an
  HA lease. Production-path v2 uses durable epochs and transactional fencing;
  HA scheduling/leadership is still an F4 gate.
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
