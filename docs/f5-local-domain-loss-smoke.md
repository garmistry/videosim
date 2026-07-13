# F5 Local Domain-Loss Smoke (2026-07-13)

## Scope

This is control-plane smoke evidence for the checked-in 1,320-stream F5
candidate. It is not F5 capacity admission.

- One Docker Desktop host ran three worker-domain Compose projects.
- Each project ran 11 workers with distinct mTLS identities.
- The control plane used one API process, PostgreSQL 17, NATS 2.11, and Nginx.
- The 1,320 external feed records were split 660 SRT and 660 DASH.
- Each worker advertised 60 total, 30 SRT, 30 DASH, 12 validation, and 2 deep
  validation tokens.
- The feed URLs were control-plane placeholders, not 1,320 independent media
  sources. This run does not prove media throughput or headroom.
- A local OAuth proxy stub replaced the production OIDC flow. Operator OIDC
  was not tested. Worker mTLS was tested.

The exact task-only server files were copied into the existing Linux app
container and the process was restarted. The worker code path was unchanged.
This is exact-source local smoke, not immutable-image or multi-host evidence.

## Baseline

The accepted baseline met all of these checks:

| Check | Result |
|---|---:|
| Fresh workers | 33 |
| Active leases | 1,320 |
| Leases per worker | 40 |
| SRT/DASH per worker | 20/20 |
| Capacity shortfall | 0 |
| Encrypted spool backlog | 0 |
| Authority changes over 30 seconds | 0 |
| Worker container restarts during baseline | 0 |

## Hard-Loss Result

All 11 `candidate-zone-a` containers received `SIGKILL`. Docker recorded the
last container exit at `2026-07-13T07:59:09.120781763Z`.

| Check | Result |
|---|---:|
| Fresh survivors | 22 |
| Active leases after recovery | 1,320 |
| Leases per survivor | 60 |
| SRT/DASH per survivor | 30/30 |
| Failed-domain streams moved | 440/440 |
| Healthy-domain streams moved | 0/880 |
| First replacement acknowledgement | 55.885 seconds |
| Last replacement acknowledgement | 64.709 seconds |
| Authority changes in 15-second recovered hold | 0 |
| Survivor container restarts | 0 |

The local smoke gate was 90 seconds from the last failed container exit to the
last replacement acknowledgement. The run passed that gate.

Two precursor runs were intentionally retained as failed evidence:

| Implementation | Last acknowledgement | Healthy streams moved | Result |
|---|---:|---:|---|
| Per-stream lease SQL | 127.445 seconds | 718 | Fail |
| Set-based offer/ack SQL only | 104.204 seconds | 718 | Fail |
| Set-based SQL plus fresh-worker placement preference | 64.709 seconds | 0 | Pass |

The final fix keeps an expired lease's last owner only as a placement
preference when the exact worker incarnation is still fresh. Expiry continues
to revoke authority; reconciliation issues a higher-epoch offer that the
worker must acknowledge.

## API And Log Checks

- Authenticated worker API v2 returned 22 workers, 60 assignments, 30 SRT, 30
  DASH, and `capacityShortfall=0` for a survivor.
- The worker TLS listener returned HTTP 400 without a client certificate.
- The operator health API returned `{"ok": true, "status": "healthy"}`.
- PostgreSQL emitted no error, fatal, panic, or warning in the fault window.
- Nginx recorded 784 requests: 751 HTTP 200 and 33 HTTP 499. The 499 responses
  were 27 report uploads, 5 heartbeats, and 1 assignment request.
- The app logged 27 matching broken pipes and no PostgreSQL, lease, or report
  conflict. Worker report spools recovered to zero queued reports.

The HTTP 499s are an open report-ingestion saturation gap. Idempotent report
IDs and encrypted spooling recovered this smoke run, but that is not a reason
to accept the timeout rate for production.

## Report-Ingestion Follow-Up

The same local topology and 1,320 placeholder feeds were rerun after durable
probe ingestion began bulk-reading feed, lease, result-ID, and current-state
fences and batch-writing current state, lease sequences, and event retention.
Immutable check-result inserts and alarm transitions remain per result.

The steady 134-second baseline committed 667 reports. Report commit latency
was 0.2200 seconds average, 0.6425 p95, 0.7545 p99, and 1.0823 maximum. Nginx
recorded 2,202 requests, all HTTP 200, and all worker spools were empty.

All 11 zone-A workers were then hard-killed. Docker recorded the last exit at
`2026-07-13T08:46:05.216876469Z`.

| Check | Result |
|---|---:|
| Fresh survivors | 22 |
| Active leases after recovery | 1,320 |
| Leases per survivor | 60 |
| SRT/DASH per survivor | 30/30 |
| Failed-domain streams moved | 440/440 |
| Healthy-domain streams moved | 1/880 |
| First replacement acknowledgement | 55.010 seconds |
| Last replacement acknowledgement | 66.396 seconds |
| Authority changes in recovered hold | 0 |
| Survivor container restarts | 0 |

The fault window contained 892 proxy requests: 891 HTTP 200, one expected
HTTP 409 stale-authority response, zero HTTP 499, and zero HTTP 5xx. The 409
report accepted 645 results and rejected three results after their
stream authority moved. Across 259 committed reports, commit latency was
0.2749 seconds average, 1.3693 p95, 1.7532 p99, and 2.4285 maximum. Nginx,
the app, PostgreSQL, NATS, the outbox publisher, and the history pruner logged
no critical error or traceback in the window, and transient spools drained.

Direct mTLS assignment checks against one survivor in each remaining domain
returned 60 streams, 30 SRT, 30 DASH, and zero shortfall. The same endpoint
without a client certificate returned HTTP 400.

This follow-up removes the reproduced local report-upload timeout symptom. It
does not close production report-ingestion saturation, and the one
healthy-owner move leaves zero-churn recovery unproven.

## Recovery-Preservation Follow-Up

A focused scheduler regression now preserves every schedulable preferred
owner during incomplete recovery, up to that worker's advertised total and
protocol capacity. This prevents protocol-target rounding from swapping a
healthy lease while failed ownership is being replaced. Full-ownership
scale-out continues to use the strict balanced targets.

The first attempted live rerun was rejected before measurement because an app
fixture correction caused worker HTTP 502/409 responses and nine container
restarts. All three disposable worker domains were removed, their state was
cleared, the lease TTL elapsed, and the accepted run began with 33 newly
created containers at `RestartCount=0`.

The accepted baseline held 1,320 active leases across 33 fresh workers at
exactly 40 streams and 20 SRT/20 DASH per worker. The ownership map did not
change during a 15-second hold, all worker report pressure snapshots were
clear, and all container restart counts remained zero.

All 11 clean zone-A containers then received `SIGKILL`. Docker recorded the
last exit at `2026-07-13T09:28:28.374732007Z`.

| Check | Result |
|---|---:|
| Fresh survivors | 22 |
| Active leases after recovery | 1,320 |
| Leases per survivor | 60 |
| SRT/DASH per survivor | 30/30 |
| Failed-domain streams moved | 440/440 |
| Healthy-domain owner/incarnation/epoch changes | 0/880 |
| First replacement acknowledgement | 54.427 seconds |
| Last replacement acknowledgement | 65.565 seconds |
| Authority changes in 15-second recovered hold | 0 |
| Survivor container restarts | 0 |

The measured API/log window contained 2,555 HTTP 200 responses and one
deliberate unauthenticated HTTP 400, with no HTTP 409, 499, or 5xx response.
Across 774 committed reports, commit latency was 0.2433 seconds average,
0.8933 p95, 1.2126 p99, and 1.3902 maximum. The app, PostgreSQL, NATS, Nginx,
outbox publisher, history pruner, and 22 survivor workers logged no critical
error or traceback. Survivor pressure reported zero queued reports, zero spool
bytes, and zero blocked workers. Two files remained on the intentionally
killed domain's local disk and were removed with the disposable fixture; they
are not claimed as recovered.

Direct mTLS assignment checks against one worker in each surviving domain
returned 22 workers, 60 streams, 30 SRT, 30 DASH, and zero shortfall. The
worker listener returned HTTP 400 without a client certificate, and the
operator health endpoint remained healthy.

This run closes the reproduced single-run healthy-owner rounding defect. The
repeated same-host follow-up is recorded below; partitions, multi-host
recovery, media capacity, and F5 admission remain open.

## Repeated Recovery Follow-Up

The checked-in F5 scheduler regression now covers zone-A loss, full 33-worker
rejoin, and a second zone-B loss. The modeled fleet returns to exact 40/20/20
placement after rejoin, then moves only the second failed domain's 440 leases
and preserves every healthy owner.

The same sequence was run against a fresh local control plane and 33 new
zero-restart worker containers. The 1,320-feed catalog again used 660 SRT and
660 DASH external placeholders, so this is control-plane evidence only.

| Phase | First acknowledgement | Last acknowledgement | Failed moved | Healthy authority changes |
|---|---:|---:|---:|---:|
| Zone-A loss | 57.711 seconds | 65.466 seconds | 440/440 | 0/880 |
| Zone-B loss after rejoin | 56.581 seconds | 66.018 seconds | 440/440 | 0/880 |

Each loss left 22 fresh survivors at exactly 60 streams, 30 SRT, and 30 DASH.
Each recovered authority map remained unchanged during its hold. Zone A then
rejoined with new process incarnations, replayed 11 stale reports that were
correctly fenced with HTTP 409, and returned the fleet to exact 33-worker
40/20/20 balance. The first and last zone-A lease acknowledgements occurred
0.256 and 164.119 seconds after the last container start. No rejoin SLO is
claimed from that settling time.

From the first loss through final API validation, Nginx recorded 7,225 HTTP
200 responses, 11 expected stale-report HTTP 409 responses, one deliberate
unauthenticated HTTP 400, and no HTTP 499 or 5xx response. Across 2,168
committed reports, commit latency was 0.1785 seconds average, 0.6275 p95,
1.6199 p99, and 5.3698 maximum. PostgreSQL, NATS, Nginx, the outbox publisher,
the history pruner, and all 22 final survivors logged no critical error. The
app logged one `BrokenPipeError` when a worker was hard-killed during a JSON
response; the shared JSON response path now ignores that completed-request
disconnect and has a focused regression.

Direct mTLS checks in the final zone-A/zone-C survivor set returned 22 workers,
60 assignments, 30 SRT, 30 DASH, and zero shortfall. Fleet pressure reported
zero queued reports, zero spool bytes, and zero blocked workers; all survivor
containers remained at `RestartCount=0`. One report remained on the killed
zone-B disk and was discarded with the fixture rather than claimed as
recovered. The unauthenticated worker request returned HTTP 400 and operator
health remained healthy.

## Network-Partition Follow-Up

The worker loop now treats exhausted assignment-fetch and lease-ack transport
retries as a disconnected cycle: it keeps the same process and incarnation,
waits one poll interval, and refetches. HTTP 409 still triggers an immediate
authority refetch, non-retryable failures still stop the worker, and one-shot
mode still fails closed. A focused regression covers both transport points.

A fresh local control plane and 33 zero-restart workers then repeated the
1,320-placeholder-feed topology. The accepted baseline held exact 40/20/20
placement with zero authority changes for 15 seconds. All 11 zone-A containers
were disconnected from their Docker network without stopping them. The last
disconnect was recorded at `2026-07-13T10:17:53.227261Z`.

| Check | Result |
|---|---:|
| Fresh survivors | 22 |
| Active leases after recovery | 1,320 |
| Leases per survivor | 60 |
| SRT/DASH per survivor | 30/30 |
| Failed-domain streams moved | 440/440 |
| Healthy authority changes | 0/880 |
| First replacement acknowledgement | 51.667 seconds |
| Last replacement acknowledgement | 61.194 seconds |
| Authority changes in 15-second recovered hold | 0 |
| Disconnected process/incarnation changes | 0/11 |
| Worker container restarts | 0/33 |

The same 11 containers were reconnected. Every PID, container start time,
database incarnation, and restart count still matched its baseline. The
encrypted stale-report backlog was fenced and drained, and the fleet returned
to exact 33-worker 40/20/20 placement. The final zone-A acknowledgement was
170.343 seconds after the last network connect; no rejoin SLO is claimed. The
rejoined authority map remained unchanged for 15 seconds.

Direct mTLS assignment checks in all three domains returned 33 workers, 40
assignments, 20 SRT, 20 DASH, and zero shortfall. Operator state reported zero
blocked workers, queued reports, and spool bytes. The worker listener returned
HTTP 400 without a client certificate, and operator health remained healthy.

From the last disconnect through the fixed validation cutoff, Nginx recorded
8,205 HTTP 200 responses, 11 expected stale-report HTTP 409 responses, one
expected lease-ack HTTP 409 during rejoin, one deliberate no-certificate HTTP
400, and no HTTP 499 or 5xx response. One operator-state request returned HTTP
401 because the disposable OAuth stub emitted the email header instead of the
user header; the stub was corrected and the state API then passed. Across
2,462 committed reports, commit latency was 0.2311 seconds average, 0.6686 p95,
1.0459 p99, and 5.8814 maximum. The app, PostgreSQL, NATS, Nginx, publisher,
pruner, and all workers had no critical error or traceback match in the fault
window.

This closes the same-host whole-domain assignment-transport partition gap. It
does not prove independent-host partitions, immutable production deployment,
representative media throughput, HA, or F5 admission.

## Two-API Catalog Follow-Up

Two direct HTTP API processes were started against one fresh PostgreSQL 17
database before any feeds were seeded. Both `/state.json` process caches held
zero streams before and after 1,320 external placeholders were inserted. This
forces durable worker assignment to use the shared catalog rather than either
replica's startup state.

The first fail-fast advisory-lock run was rejected: it produced 219 scheduler
contention retries, and 27 assignment requests needed at least the worker's
default five-attempt retry window. The scheduler transaction now waits on the
tenant advisory lock. The accepted rerun produced:

| Check | Result |
|---|---:|
| Unique assigned streams | 1,320/1,320 |
| Workers | 33 |
| Streams per worker | 40 |
| SRT/DASH per worker | 20/20 |
| Assignment streams served by API A/API B | 680/640 |
| Scheduler contention retries | 0 |
| Requests beyond the worker retry window | 0 |
| Cross-replica ownership changes | 0 |
| Assignment latency p50/p95/p99/max | 0.2674/0.5144/1.5733/1.5733 seconds |

Each worker's assignment was then fetched through the opposite API process;
every owner stayed unchanged and every lease remained active. PostgreSQL held
33 fresh active workers plus 1,320 acknowledged, active, unexpired leases, with
no non-active lease. Both API containers and PostgreSQL stayed running at zero
restarts, both health/readiness APIs returned HTTP 200, and application logs
contained no error or traceback.

This is local shared-catalog and scheduler-serialization evidence only. The
feeds were non-probed placeholders, security was disabled, requests targeted
the replicas directly without a load balancer, and PostgreSQL was a single
instance. Replicated operator state, generated-feed ownership, authenticated
rolling/failover behavior, HA storage, media capacity, and F5 admission remain
open.

## Paginated Operator Catalog Follow-Up

`GET /api/operator/feeds` now reads persisted feed configuration and config
versions directly from PostgreSQL instead of one API process's startup cache.
The versioned response uses an ID cursor, defaults to 100 rows, and rejects
limits outside 1-200. Trusted-proxy mode requires the existing OIDC viewer
identity, and invalid durable rows fail the complete request with HTTP 503.

Two API processes were started against an empty fresh PostgreSQL 17 database.
Both process-local `/state.json` views contained zero feeds before and after
1,320 external placeholders were seeded: 660 SRT and 660 DASH. A page walk then
alternated requests between the two APIs.

| Check | Result |
|---|---:|
| Unique catalog feeds | 1,320/1,320 |
| SRT/DASH feeds | 660/660 |
| Pages | 7 |
| Page sizes | 200/200/200/200/200/200/120 |
| API A/API B pages | 4/3 |
| Page latency p50/p95/max | 0.0063/0.0079/0.0167 seconds |
| Maximum response size | 75,709 bytes |
| Total response bytes | 484,070 bytes |
| API/database container restarts | 0/0/0 |

Every ID was globally sorted and appeared once, every config version was one,
both APIs remained healthy, and their logs contained only the startup line.
PostgreSQL logged no runtime error after readiness. Focused tests also prove
SQLite paging, bounds, viewer authorization, post-start create/update/delete
visibility from a stale-cache replica, and fail-closed malformed/overlong-row
behavior.

This is a read-only configuration API, not a completed operator read model.
The cursor does not hold a database snapshot across requests, the React GUI
still polls unbounded process-local `/state.json`, and feed-detail/runtime
state, concurrent create-ID allocation, load-balanced mutations, generated
runtime ownership, a real load balancer, HA storage, and F5 admission remain
open. The live page scan used security-off direct HTTP; authorization evidence
is from the trusted-proxy HTTP regression.

## Validation

- `python3 -m unittest tests.test_cert_script`: 1 passed.
- Fresh container unit discovery: 383 ran, 86 were environment-gated skips,
  and none failed.
- Fresh PostgreSQL database: 46 ran, 4 runtime-role checks skipped, and none
  failed.
- Focused F5 scheduler contract: 8 passed.
- Startup API/media/log workflow in no-build mode: 1 passed in 14.558 seconds.
- Fresh-image startup was attempted first, but Docker Buildx made no progress
  before container creation for more than three minutes. It was canceled; the
  successful startup run reused existing local images.
- Follow-up project-image unit discovery: 384 passed with 87 environment-gated
  skips.
- Follow-up isolated PostgreSQL suite: 47 passed with 4 runtime-role skips,
  including the 60-stream/120-result query-bound check.
- Follow-up startup API/media/log workflow in no-build mode: 1 passed in 13.348
  seconds.
- Recovery-preservation focused GUI suite: 80 passed.
- Recovery-preservation project-image unit discovery: 385 passed with 87
  environment-gated skips in 24.760 seconds.
- Recovery-preservation startup API/media/log workflow in no-build mode: 1
  passed in 13.502 seconds.
- Repeated F5 model plus GUI focused suites: 90 passed.
- Repeated-recovery project-image unit discovery: 386 passed with 87
  environment-gated skips in 24.731 seconds.
- Exact `gui.py` hash was overlaid on the existing local app image after Docker
  Buildx again stopped at base-image metadata resolution; the no-build startup
  API/media/log workflow then passed in 13.514 seconds.
- Partition-resilience worker/CLI/spool suites: 52 passed.
- Partition-resilience project-image unit discovery: 387 passed with 87
  environment-gated skips in 24.329 seconds.
- Exact `worker.py` hash was overlaid on the existing local worker image; the
  no-build startup API/media/log workflow passed in 11.909 seconds.
- Shared-catalog follow-up: exact task-only unit discovery passed 389 tests with
  89 environment-gated skips; a fresh PostgreSQL database passed 74 durable
  store/worker-v2 tests with 4 runtime-role skips.
- Hash-verified exact app/worker source overlays passed the marked no-build
  startup API/media/log workflow in 12.038 seconds. A retained artifact rerun
  passed API readiness, worker registration, feed create/start, normal SRT
  video/audio/captions validation, stop, Compose-state capture, and Docker-log
  capture in 8.318 seconds.
- Paginated operator follow-up: exact task-only unit discovery passed 394 tests
  with 92 environment-gated skips in 24.678 seconds; a fresh PostgreSQL database
  passed 77 durable store/worker-v2 tests with 4 runtime-role skips in 19.812
  seconds. Hash-verified exact app/worker source overlays passed the no-build
  startup API/media/log workflow in 11.915 seconds.
- Documentation contract: 4 passed.

## Remaining Gates

- Run the three domains on independent Linux hosts with immutable image digests
  and production certificates.
- Exercise production OIDC, replicated API/scheduler instances, HA PostgreSQL,
  and HA NATS.
- Sustain representative report-ingestion load with lock-wait, deadlock,
  latency, timeout, and spool-recovery evidence.
- Run repeated loss and partitions on independent hosts to establish a
  production zero-churn SLO or define and admit an explicit churn budget.
- Use independent media sources and prove validation freshness, resource
  headroom, alarm consistency, and backpressure during failure.
- Produce the required clean, hashed, no-skip 24-hour evidence bundle and pass
  `python3 -m videosim capacity-check` against `scale/policies/f5-1000.json`.
