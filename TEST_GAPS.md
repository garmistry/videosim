# TEST_GAPS.md

# Test Gaps

## Current Gate

Product Milestones 0 through 11 have no known P0 test gaps. Milestone 1 through
11 live SRT receiver proof runs in Docker with
`docker compose run --build --rm live-srt`. The separate production-distribution
F2 gate is explicitly incomplete below.

## Deferred Until Later Milestones

The following gaps are expected because their owning milestone is not complete
or the gap is non-critical for a completed milestone:

| Gap | Priority | Planned milestone |
|---|---|---|
| Audio continuity for 30 minutes. | P1 | Milestone 2 |
| Caption text update-over-time verification. | P1 | Milestone 3 |
| Normal feed 24-hour soak run. | P0 | Milestone 12 |
| Required outage mode long soak runs. | P0 | Milestone 12 |
| GUI responsiveness during long soak. | P0 | Milestone 12 |
| 24-hour resource threshold evidence. | P1 | Milestone 12 |

These are not skipped for their owning milestones. They are blocked by missing
implementation and must pass before those milestones advance.

## Production Security

- Trusted-proxy identity, worker-ID matching, viewer/admin authorization, input
  limits, destination policy, TLS client context, proxy configuration, and a
  live local mTLS accept/identity-mismatch smoke are covered.
- A real organization OIDC tenant/login/group-claim flow has not been exercised;
  tests inject the headers oauth2-proxy is configured to emit.
- Automated CA issuance, certificate rotation/revocation, durable multi-tenant
  identity/resource grants, firewall policy deployment, and an external/WORM
  audit archive remain future durable/HA gates. PostgreSQL now selectively
  persists auth/authz denials and successful persistent feed/profile mutations,
  but allowed reads/workers/local runtime actions remain stdout-only and denial
  persistence is best-effort during a database/outbox outage.
- Application DNS/address checks are defense in depth. DNS rebinding and HTTP
  redirects require VM egress enforcement and future redirect-aware fetch
  policy tests before external production.

## Distributed Architecture

- Foundation coverage now includes versioned process-instance/generation/token
  fencing, server-derived report ownership, retained-state pruning, independent
  heartbeat during a probe batch, typed worker API conflicts/validation/storage
  errors, scoped probe metrics, concurrent assignment/allocation behavior, and
  an in-process assignment/report benchmark.
- PostgreSQL/JetStream integration coverage now proves migration safeguards,
  feed version/import behavior, fresh offered/acknowledged worker leases,
  observation-time and inconclusive-alarm fencing, idempotent
  result/alarm/audit/outbox transactions, simultaneous reports, concurrent
  outbox claims, and durable JetStream publish acknowledgements. A local custom-format
  backup/restore plus semantic comparison also passed.
- PostgreSQL HTTP worker v2 now uses durable incarnation/offer/ack/heartbeat/
  epoch/config/per-lease-sequence/result fences. Catalog monitor observations
  atomically project server-timed pending/current alarms, bounded event edges,
  and direct PostgreSQL `/state.json` reads; stale duplicate, reassigned,
  expired, and config-revoked reports cannot rerun that projection. SQLite
  retains process-local v1 and file-backed monitor state for lab use.
  Selective durable HTTP audit wiring now has atomic persistent feed/profile
  writes, denial records, database immutability, and non-owner service roles;
  inbox-deduplicated consumer/projection replay and replay parity, ack-before-
  mark recovery beyond the broker dedup window, DB/broker
  restore combinations, schema upgrade under load, PITR, measured RPO/RTO, and
  HA remain open F2/F4 P0 gates.
- The control-plane benchmark runs no SRT/DASH probes. Representative media
  load, failure storms, 24-hour soak, security, restore, and 1,000/5,000/10,000
  admission evidence remain missing and must not be inferred from it. The F5
  policy and `capacity-check` now fail closed on missing/tampered artifacts,
  weak workload/headroom declarations, skipped checks, or incomplete admission
  criteria; this verifier is not the missing workload or evidence bundle.
- Worker heartbeats now expose completed-batch CPU delta, cumulative worker and
  child peak RSS, and Linux post-batch descriptor count. There is no time-series
  retention, child aggregate/peak-concurrency RSS, media byte/socket accounting,
  representative per-check baseline, or saturation curve yet. `worker-benchmark`
  can produce the single-worker real-probe report from an exported state, but no
  representative independent-source result has been committed or admitted.
- The manifest-driven DASH and SRT fixture fleets have unit and one-cycle Linux
  coverage for healthy, slow, dead, and malformed endpoints. The
  deterministic mixed composer can consume multiple URLs per behavior and
  derives whether any are shared. The checked-in 500-SRT and 500-DASH fleets
  have run simultaneously and composed 1,000 distinct URLs; all 400 healthy SRT
  sockets passed one full media validation, all 400 healthy DASH paths returned
  an MPD, and one DASH path passed full media validation. Healthy SRT relays
  share one encoded source and DASH paths share one origin/generator. Repeated
  reconnect cadence, independent sources, the generated/external cross-product,
  concurrent storms, and long-run fixture reliability remain open.
- The worker benchmark can require and report unique validation-start coverage
  plus repeated cycle and wall-time cadence. A 250-cycle Linux run against 1,000
  distinct SRT/DASH URLs covered every stream at least twice with eight
  admissions per cycle. Full first coverage was bounded at 125 cycles and
  87.426 seconds; the worst initial/repeat/trailing gap was bounded at 125 cycles
  and 89.651 seconds, passing the configured 90-second gate. The seconds values
  conservatively span cycle boundaries rather than timestamping each probe, and
  the 30 ms stream, 1 ms aggregate, and 90-second limits are test criteria, not
  approved freshness SLOs. Healthy URLs still share one source per protocol;
  this is not independent media load, a soak, or a capacity curve.
- Checked-in 660-SRT, 660-DASH, and mixed-1,320 manifests provide a 32%
  headroom regression input. A ten-worker Linux run split it into balanced
  132-stream, 66/66 protocol shards. With five-second stream budgets, all URLs
  received at least two attempts and first coverage stayed at or below 86.404
  seconds, but two workers failed the 90-second repeated-gap gate and the worst
  upper bound was 91.473 seconds. Outcomes were 1,056 success, 66 issue, and
  1,598 timeout; SRT fixture memory rose from 2.717 GiB before load to 3.732
  GiB afterward. This is failed single-host saturation evidence, not actual
  failure-domain loss, sufficient healthy-outcome coverage, or F5 admission.
- Worker reports now separate validation outcomes by protocol. A low-load
  8-SRT/8-DASH repeated calibration showed DASH at 16/16 success while SRT
  produced 8 success/8 timeout with a five-second budget. Raising the budget to
  15 seconds produced 16/16 SRT and 16/16 DASH success, with 9.845-10.013 second
  cycles. The 1,320 run's five-second SRT outcomes are therefore under-budget
  evidence; representative protocol budgets and a new headroom run remain
  required.
- Process-local failure injection covers complete survivor assignment and stale
  report rejection for 1,300 logical streams after one of ten workers is
  removed. It also exposes 1,044 excess assignment moves above the 130 required;
  the durable scheduler now limits the equivalent modeled loss to the 130
  required moves while preserving scale-out rebalance. A one-second DB-time
  HTTP test covers survivor heartbeat renewal, failed worker/lease expiry,
  higher-epoch reassignment, and stale-report rejection. Production 60-second
  recovery timing, partitions, and real failure-domain recovery remain open.
- The v2 heartbeat renews durable membership and matching active leases, but
  still lacks retry/backoff metrics. Static `capacity.maxStreams` admission is
  covered, and bounded stream-level concurrency has unit coverage, but worker
  admission can now enforce operator-set total/SRT/DASH counts. Execution bounds
  submissions to one phase-specific concurrency window, rotates validation
  deferred by the aggregate budget, and hard-bounds built-in media
  subprocess/poll waits. Validation/deep tokens are operator settings on one
  executor, not measured weighted-cost pools. Arbitrary-checker and
  trickle-resistant HTTP cancellation, tenant fairness, durable fleet queue
  backpressure, measured media capacity, and 1,000-stream failure/soak evidence
  remain open.
- Lease offer/renew and acknowledgement arrays now remove per-lease transaction
  overhead and roll back atomically, but still issue per-stream SQL inside each
  transaction. Tenant-scoped PostgreSQL transaction leadership now serializes
  assignment decisions and rolls back on scheduler-session loss. Production
  PostgreSQL saturation, lock-wait/deadlock metrics, persistent leadership,
  replicated deployment, and 1,000-stream poll cadence remain open.
- Concurrent workers complete admitted validation windows before starting
  TR-101/frame-rate/loudness work, with deterministic phase-order coverage. A
  configured cadence staggers deep work; aggregate budget exhaustion stops new
  validation starts, preserves alarms with inconclusive observations, rotates
  deferred streams, and defers the deep phase. Black/frozen validation cost,
  weighted check-cost/tenant fairness, durable queue backpressure, and
  slow-stream-storm freshness evidence remain open. Heartbeats now expose a
  schema-bounded latest cycle/deferral/spool pressure snapshot, and assignment
  excludes spool-blocked workers with explicit shortfall. Pressure history,
  hysteresis, alerts, recovery SLO evaluation, and production saturation
  evidence remain open. The 1,300-assignment failure-domain test proves only
  scheduler math for a 1,000 target plus 30% modeled headroom.
- The environment-gated Compose startup workflow proves one worker plus one
  normal SRT feed through the real HTTP API, captures Docker state/logs, and has
  a documented Chrome path. It does not exercise the production PostgreSQL/NATS
  overlay, worker-loss recovery, soak duration, or 1,000-stream media load.
- Graceful worker drain is final-report ordered and incarnation/lease fenced.
  Worker-v2 report spooling is encrypted, byte-bounded, write-ahead, and pauses
  new probes until replay succeeds or the server fences stale authority. Real
  control-plane outage/restart, disk-fill, key rotation, corrupted-entry repair,
  and spool recovery under representative load remain open F3 gates. Hard-kill
  reassignment still depends on lease expiry.

## Monitoring

- TR 101 290 priority 1/2 indicators and parser-backed priority 3 PSI/SI plus
  unreferenced-PID and T-STD timing indicators have unit coverage, and malformed
  DASH fixture sampling is covered through the monitor alarm path for every
  indicator. A Docker `monitor-fixtures` gate is defined for that fixture suite.
  The SRT fixture matrix produces malformed live transport and has one-cycle
  sync-loss/sync-byte evidence, but not end-to-end alarm proof for every
  indicator.
- Audio loudness alarms for ITU-R BS.1770 measurement availability, EBU R 128,
  and ATSC A/85 have unit coverage. Full-program loudness compliance runs are
  not part of the live monitor slice; the monitor samples live audio for alarm
  detection.
- Frame-rate selection and mismatch alarms have unit coverage for GUI/CLI,
  profile loading, GStreamer caps, FFprobe parsing, and monitor alarm behavior.
  Long-running live SRT/DASH frame-rate drift fixtures are not generated yet.
- Per-stream alert-profile UI, enable/disable lifecycle, filtering, and delay
  have unit coverage through the monitor state path and GUI payload/rendering.
  There is no persisted profile store beyond the running GUI process yet.
