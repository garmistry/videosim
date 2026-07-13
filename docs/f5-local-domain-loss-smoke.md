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

The server files from commit `461c31e` were copied into the existing Linux app
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

## Remaining Gates

- Run the three domains on independent Linux hosts with immutable image digests
  and production certificates.
- Exercise production OIDC, replicated API/scheduler instances, HA PostgreSQL,
  and HA NATS.
- Remove or admit the observed report-upload timeout rate under representative
  load.
- Use independent media sources and prove validation freshness, resource
  headroom, alarm consistency, and backpressure during failure.
- Produce the required clean, hashed, no-skip 24-hour evidence bundle and pass
  `python3 -m videosim capacity-check` against `scale/policies/f5-1000.json`.
