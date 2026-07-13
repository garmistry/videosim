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

This run closes the reproduced single-run healthy-owner rounding defect. It
does not prove repeated loss, partitions, multi-host recovery, media capacity,
or F5 admission.

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
- Documentation contract: 4 passed.

## Remaining Gates

- Run the three domains on independent Linux hosts with immutable image digests
  and production certificates.
- Exercise production OIDC, replicated API/scheduler instances, HA PostgreSQL,
  and HA NATS.
- Sustain representative report-ingestion load with lock-wait, deadlock,
  latency, timeout, and spool-recovery evidence.
- Repeat clean domain-loss and partition runs to establish a zero-churn SLO or
  define and admit an explicit churn budget.
- Use independent media sources and prove validation freshness, resource
  headroom, alarm consistency, and backpressure during failure.
- Produce the required clean, hashed, no-skip 24-hour evidence bundle and pass
  `python3 -m videosim capacity-check` against `scale/policies/f5-1000.json`.
