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
  criteria. `scale/workloads/f5-1000-candidate.json` passes that workload
  contract with 33 workers across three domains and exact 1,320/660/660
  total/SRT/DASH survivor tokens after one domain loss. `verify-assignments`
  now captures one consistent PostgreSQL assignment snapshot and verifies the
  exact baseline/survivor shape, single current authority, balance, and
  tenant-matched baseline-bounded ownership movement. Exact synthetic and
  PostgreSQL-backed tests cover 1,320 streams, 33 baseline workers, 22
  survivors, and 440 required moves; they do not replace the missing
  independent-host capture, physical failover timing, media evidence, or soak.
  `verify-alarm-consistency` now reconciles latest results, current check state,
  pending/current alarms, retained transition payloads, and matching outbox rows
  from one read-only repeatable snapshot. Its P0 PostgreSQL test covers all
  1,320 desired streams, proves a timeout cannot explain an active-alarm clear,
  and detects a deliberately unexplained false clear. The durable load harness
  now commits a synthetic all-1,320 `feed_reachable` fault and recovery, checks
  exact current/pending/raised/cleared state, and retains 2,640 matching alarm
  outbox transitions. This is PostgreSQL projection pressure only: no
  independent-host endpoint-fault capture, media-derived transition latency,
  broker/consumer delivery proof, retention-window soak, or 24-hour report
  exists yet.
  `control-plane-load` now drives the exact 1,320-stream/33-worker shape through
  real PostgreSQL lease, heartbeat, fenced-result, current-state, and result-
  outbox transactions in an empty disposable database. Its P0 integration test
  proves one complete two-profile tick and retained evidence. A second P0 test
  stops one synthetic domain, waits for real PostgreSQL freshness expiry, uses
  the production scheduler to move exactly its 440 streams to 22 survivors,
  preserves all healthy owners, rejects a stale failed-owner report, and
  resumes complete reporting. The candidate default is now a configurable
  30-second freshness boundary with five-second heartbeats, and retained exact
  PostgreSQL evidence recovered at 29.888/29.889-second p95/p99. That synthetic
  result is not sustained or physical deployment-timing evidence. No 24-hour
  run, worker HTTP/mTLS domain loss, broker consumption, real endpoint-fault
  behavior, media freshness, physical-domain timing, or independent-host
  resource curve has passed.
  Three local Compose
  domains passed a PostgreSQL control-plane hard-loss smoke, but the candidate
  has not run on independent hosts or passed media/headroom/24-hour admission.
  The initial local smoke moved only the failed domain's 440 streams. A
  report-ingestion follow-up moved all 440 failed-domain streams plus one of
  880 healthy-domain streams. A recovery-preservation regression and clean
  rerun then moved all 440 failed-domain streams with zero owner/incarnation/
  epoch changes across the 880 healthy streams in 65.565 seconds. A subsequent
  same-host loss/rejoin/second-loss run recovered in 65.466 and 66.018 seconds
  with zero healthy authority changes in both cycles. A same-host whole-domain
  network partition then kept all 11 isolated processes/incarnations alive,
  recovered their 440 leases in 61.194 seconds with zero healthy authority
  changes, drained fenced stale reports after reconnect, and returned to exact
  40/20/20 balance. Independent-host partitions/repeated recovery and a
  production churn SLO remain unproven, and none of the local smokes is the
  missing approved evidence bundle.
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
- Three checked-in 220-SRT/220-DASH fixture-domain shards now compose the exact
  1,320-URL candidate. The Linux startup workflow now fails unless generated
  state exactly matches each selected manifest with unique IDs/endpoints, then
  samples one healthy URL per protocol and retains Docker resources/state/logs.
  One local exact-source image run booted a full 220+220 shard and recorded
  220 distinct endpoints per protocol with the exact 176/22/11/11 behavior
  split. It sampled only two media paths and ran on one Docker VM; three
  independent hosts and all-path media load remain open. A follow-up physically
  stopped both full-shard source containers, observed sampled healthy SRT/DASH
  paths become issues in 16.595 seconds, and recovered both in 3.446 seconds
  after restart. That still proves only two paths on one VM, not all 440 worker
  observations/alarms, per-stream source independence, concurrent reconnect
  pressure, independent source-domain loss, or sustained capacity.
- Composed fixture states can now be imported into an exclusive PostgreSQL feed
  catalog through the production feed-generation and lease semantics. The
  importer validates complete external-feed configs, rejects shared endpoints
  and unlisted rows by default, uses one transaction, verifies persisted
  parity, and retains a report that cannot claim capacity. A local exact
  1,320-row run changed every row in 4.428 seconds, changed zero on rerun, and a
  newly booted API returned all 1,320 unique source-matching endpoints in seven
  pages with zero container restarts and clean logs. This closes the missing
  scenario-to-worker-catalog bridge, not the independent three-host all-path
  media, alarm-transition, reconnect-headroom, HA, or 24-hour admission gaps.
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
- A calibrated 1,320-URL run used 22 concurrent balanced workers, 60 streams
  and 12 validation tokens each, plus a 15-second stream budget. All streams
  received at least two attempts, but only eight workers passed the 90-second
  gap gate and the worst upper bound was 99.052 seconds. Protocol outcomes were
  SRT 41 success/124 issue/1,155 timeout and DASH 1,056 success/66 issue/198
  timeout. The single host reached 5.31 GiB, 7,386 processes/threads, and about
  954% worker CPU before fixture CPU. The checked-in 33-worker candidate moves
  this survivor shape across three domains. Its local control-plane hard-loss
  smoke passed, but multi-host media capacity remains untested and is required
  before another F5 claim.
- The capacity-aware scheduler model now places the 1,320-stream candidate at
  40 streams per worker before loss and exact 60-stream, 30-SRT/30-DASH loads
  on each of 22 survivors afterward. Only the failed domain's 440 assignments
  move. A local PostgreSQL/33-container hard-kill smoke reproduced that exact
  result, with the final replacement acknowledged in 64.709 seconds and no
  survivor restart. A report-ingestion follow-up recovered in 66.396 seconds
  but also moved one healthy stream. The subsequent recovery-floor fix retained
  all 880 healthy owner/incarnation/epoch tuples while replacing 440 failed
  leases in 65.565 seconds. A second local run repeated that result across two
  distinct domain losses separated by full rejoin. A third local run isolated
  one domain at the Docker network boundary, preserved all 11 process
  incarnations, recovered exact survivor placement in 61.194 seconds, and
  rejoined without restarts. It still does not exercise independent-host
  partitions, representative media probes, or separate infrastructure hosts.
- `docker-compose.worker-domain.yml` now renders one hardened 11-worker domain
  with unique mTLS identities and the candidate's exact admission/concurrency
  limits. `worker-domain-startup.py` now validates one host's candidate zone,
  11 certificate CN/key pairs, private-file modes, immutable image reference,
  host and worker-path health APIs, exact stable process set, image identity,
  restart counts, and Docker logs. A marked local PostgreSQL-backed run passed
  all startup checks with 11 active 60/30/30 workers and clean spools, but used
  a mutable local image and HTTP control path. The domain has not run with a
  production digest and HTTPS/mTLS path on three independent hosts; host sizing,
  media load, physical domain loss, and admission evidence remain open.
- Process-local failure injection covers complete survivor assignment and stale
  report rejection for 1,300 logical streams after one of ten workers is
  removed. It also exposes 1,044 excess assignment moves above the 130 required;
  the durable scheduler now limits the equivalent modeled loss to the 130
  required moves while preserving scale-out rebalance. A one-second DB-time
  HTTP test covers survivor heartbeat renewal, failed worker/lease expiry,
  higher-epoch reassignment, and stale-report rejection. The local 60-second
  TTL hard-loss path recovered twice in 65.466/66.018 seconds around a full
  domain rejoin. Partitions, multi-host timing, and production failure-domain
  recovery remain open.
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
- Lease offer/renew and acknowledgement arrays now use a worker-fence query and
  one set-based PostgreSQL lease statement per request while retaining atomic
  rollback. Tenant-scoped PostgreSQL transaction leadership queues concurrent
  assignment decisions and rolls back on scheduler-session loss. Two local API
  processes started with empty caches, then assigned a post-start 1,320-feed
  PostgreSQL catalog exactly once across 33 workers with zero scheduler retries
  and zero cross-replica ownership changes. This covers external-feed worker
  assignment only: operator mutations/runtime state, generated-feed process
  ownership, a production load balancer, bounded lock-wait telemetry/timeouts,
  and API/DB HA remain open. An earlier local fault window recorded 33 client
  timeouts, mostly report uploads,
  despite successful spool recovery. Probe-report ingestion now bulk-reads its
  fences and batch-writes mutable projections; a same-shape follow-up recorded
  zero HTTP 499/5xx, one stale-authority 409, 1.3693-second report commit p95,
  and eventual empty spools. Sustained report saturation, lock-wait/deadlock
  metrics, persistent leadership, replicated deployment, and sustained
  1,000-stream poll cadence remain open.
- The authenticated operator read model now combines a fail-closed 200-row
  maximum keyset catalog, a lightweight overview without stream/probe arrays,
  and one-feed scoped detail. The React root uses 100-row pages instead of
  durable `/state.json`; a local alternating two-API walk returned all 1,320
  post-start rows exactly once in 14 pages while both process caches stayed
  empty. A follow-up started both replicas after all 1,320 rows existed; both
  still cached zero feeds, and concurrent durable creates used distinct UUID-
  backed IDs while a forced collision failed closed. The cursor is not a
  cross-request database snapshot. Durable configuration forms now require the
  displayed positive version; any replica can update/alert/delete an external
  feed, stale update/delete fails with HTTP 409, and remote external detail is
  writable with runtime unknown. A local two-API smoke retained zero caches
  while mutating one of 1,320 rows and paging the remaining 1,319 exactly once.
  Generated runtime ownership, idempotent create/retry, and HA remain open F4
  gates.
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
