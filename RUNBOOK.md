# RUNBOOK.md

# Runbook

## Install Dependencies

```sh
scripts/install-deps.sh
```

Supported paths:

- Linux: apt, dnf, or pacman based distributions.
- macOS: Homebrew.

The script installs Python/Tk, FFmpeg/ffprobe, and GStreamer components needed
for the planned GUI, SRT output, MPEG-TS muxing, H.264 video, and captions.

## Verify Current Repository Contract

```sh
python3 -m unittest tests.test_docs_contract tests.test_cli_video_feed tests.test_live_srt
```

Expected result: all current contract and CLI checks pass; the live SRT test is
skipped unless `VIDEOSIM_LIVE_SRT=1` is set.

Run the Linux/Docker gates:

```sh
docker compose run --build --rm test
docker compose run --build --rm live-srt
```

## Run The Current CLI

Start an audio/video SRT listener feed:

```sh
python3 -m videosim start --port 9000
```

Change the generated audio tone:

```sh
python3 -m videosim start --port 9000 --audio-frequency 1000
```

Disable audio for the earlier video-only path:

```sh
python3 -m videosim start --port 9000 --no-audio
```

Disable generated CEA-608 captions:

```sh
python3 -m videosim start --port 9000 --no-captions
```

Start from the sample normal profile:

```sh
python3 -m videosim start --profile profiles/srt-normal.yaml
```

Start a DASH feed. The process writes `manifest.mpd`, MPEG-TS segments, and
`captions.vtt` when captions are enabled:

```sh
python3 -m videosim start --profile profiles/dash-normal.yaml --dash-dir /tmp/videosim-dash
```

Available static profiles:

```text
profiles/srt-normal.yaml
profiles/srt-audio-only.yaml
profiles/srt-video-only.yaml
profiles/srt-no-captions.yaml
profiles/srt-black-video.yaml
profiles/srt-frozen-video.yaml
profiles/dash-normal.yaml
profiles/dash-audio-only.yaml
profiles/dash-video-only.yaml
profiles/dash-no-captions.yaml
profiles/dash-black-video.yaml
profiles/dash-frozen-video.yaml
```

## Validate A Running Feed

```sh
python3 -m videosim validate --profile profiles/srt-normal.yaml --port 9000
python3 -m videosim validate --profile profiles/srt-normal.yaml --port 9000 --json
python3 -m videosim validate --profile profiles/dash-normal.yaml --dash-dir /tmp/videosim-dash --json
```

## Run A Soak

Short smoke:

```sh
python3 -m videosim soak --profile profiles/srt-normal.yaml --port 9000 --duration-seconds 60 --validation-interval-seconds 15 --json
```

Full M12 normal-feed run:

```sh
python3 -m videosim soak --profile profiles/srt-normal.yaml --port 9000 --duration-seconds 86400 --validation-interval-seconds 900 --json
```

Full M12 feed soak runner:

```sh
scripts/run-m12-soak.sh
docker compose run --build --rm m12-soak
```

Check completed soak reports:

```sh
python3 -m videosim soak-check --report-dir reports/m12-soak --memory-growth-threshold-mb 200
```

Run GUI responsiveness soak:

```sh
python3 -m videosim gui-soak --duration-seconds 86400 --validation-interval-seconds 900 --poll-interval-seconds 30 --json
docker compose run --build --rm m12-gui-soak
```

## Launch The GUI

```sh
python3 -m videosim gui --http-port 8080 --feed-port 9000
```

Open `http://127.0.0.1:8080`.

The GUI starts with zero configured feeds. Use Create feed first, then use the
active-feed table for CRUDL operations:

- Create a named generated SRT/DASH feed or register an external SRT/DASH URL.
- List all configured streams.
- Open/read a stream at `/feeds/<stream-id>` to see endpoint, status, logs, and validation output.
- Update the selected stream name, source, protocol, and the source-specific fields.
- Delete the selected stream.

GUI feed registrations are stored in SQLite when the GUI is launched through
`python3 -m videosim gui`. The default path is
`$XDG_DATA_HOME/videosim/feeds.sqlite3` or
`~/.local/share/videosim/feeds.sqlite3`; set `VIDEOSIM_DB_PATH` to override it.
The database stores feed definitions and alert profiles, not local subprocess
handles or transient runtime metrics.

Feed detail pages are directly bookmarkable and do not include the create-feed
form; use `/` for the create/list workflow. Generated feeds show protocol,
mode, and frame-rate fields. Use them to start normal, audio-only, video-only,
no-captions, black-video, or frozen-video feeds. External feeds show protocol
and URL fields only; register a bring-your-own SRT endpoint or DASH manifest
URL for validation and alerting. Generated feeds have independent start, stop,
validate, and copy URL actions. External feeds expose validate, copy URL, and
alert profile actions only. Use the runtime fault controls on generated feeds
to toggle video, audio, captions, black video, or frozen video while the GUI is
running; the MVP applies those changes with a controlled stream restart.

Use the Validate button to run the current profile validation from the GUI.
External validation probes the registered URL and reports whatever tracks are
present without applying mode or frame-rate expectations.
Use Download diagnostics to export status, mode, endpoint, last error,
validation output, and recent logs as text.

In SQLite lab mode, the React GUI polls `/state.json` every second and shows
per-feed estimated bit rate, outbound total data, uptime, and generated video
frame count. In PostgreSQL mode, the root pages 100 feed configurations at a
time through `/api/operator/feeds`, polls `/api/operator/overview`, and polls
one selected feed through `/api/operator/feeds/<feed-id>`. Runtime estimates
and five-minute traffic graphs are available only when the serving process has
current local runtime state. Use Validate to prove actual receiver-visible
stream state.

PostgreSQL API startup intentionally leaves the process feed cache empty even
when the durable catalog already contains feeds. New durable feeds use UUID-
backed `stream-<32-hex-characters>` IDs so independent replicas do not allocate
the same sequential ID; PostgreSQL still rejects a forced collision through
the existing expected-version insert fence. SQLite keeps sequential `stream-N`
IDs and startup reload behavior.

For a bounded durable feed-catalog read, page the operator API instead of
loading every feed through `/state.json`:

```sh
curl -fsS 'http://127.0.0.1:8080/api/operator/feeds?limit=200'
curl -fsS 'http://127.0.0.1:8080/api/operator/feeds?limit=200&cursor=stream-0200'
curl -fsS 'http://127.0.0.1:8080/api/operator/overview'
curl -fsS 'http://127.0.0.1:8080/api/operator/feeds/stream-0201'
```

Follow `nextCursor` while `hasMore` is true. The maximum and default limits are
200 and 100. Production requests use the same OIDC viewer authorization as the
GUI. Catalog rows return persisted configuration/config versions only. Detail
returns exactly one feed and stream-scoped monitor data. A replica without a
matching local config version returns `runtimeKnown=false`. External feeds stay
configuration-writable on that replica; generated feeds return
`operatorReadOnly=true` until generated runtime ownership is durable.

Every PostgreSQL-backed update, alert-profile, expectation, or delete form must
send the positive `config_version` returned by the detail/catalog read. Missing
or malformed versions return HTTP 400. A stale version or a generated-feed
request sent away from its runtime owner returns HTTP 409 and commits no feed or
success-audit change. External-feed update, alert-profile, and delete requests
may be sent to any healthy API replica; reload detail after a 409 before asking
an operator to retry.

Video-present modes include a running clock overlay in the encoded video so
receivers can visually prove live motion and timing.

When a running mode has video, table thumbnails and the preview dialog use
GStreamer to render a local frame matching the active mode without attaching
another receiver to the SRT listener. Use Validate to prove actual stream state.

## Deploy With Docker Compose

```sh
docker compose up --build app
```

Open `http://127.0.0.1:8080`. The app service publishes the GUI on TCP 8080 and
SRT listeners on UDP 9000-9010 by default. The GUI and feed subprocesses run
inside the `videosim-app-1` container. Override the primary host ports with
`VIDEOSIM_HTTP_PORT` and `VIDEOSIM_FEED_PORT`.

The GUI root view is an active-feed table styled from the `design_docs` model:
IBM Plex Sans for UI, IBM Plex Mono for data, warm dark/light neutrals,
broadcast amber actions, and dot-plus-label status. Use Create feed to open the
modal, and click a row preview thumbnail to open the larger feed preview.

Generated SRT listener pipelines accept receiver clients at
`srt://127.0.0.1:9000?mode=caller` and keep running when receivers disconnect.
Do not add a `maxconn` URI option to the GStreamer `srtsink` command in this
Docker image; the packaged plugin does not expose that as a supported property
and it can crash the listener.

Generated DASH feeds are served by the same GUI HTTP server at
`http://127.0.0.1:8080/dash/<stream-id>/manifest.mpd`.

### Startup validation workflow

Run the environment-gated deployment smoke before a demo or release candidate:

```sh
scripts/startup-validation.py
```

The workflow uses the isolated Compose project `videosim-startup-validation`
on HTTP port 18080 and feed port 19000 by default. It builds the app and worker,
waits for `/readyz`, verifies worker registration through `/state.json`, creates
and starts a normal SRT feed through the operator HTTP forms, invokes the real
validator, requires reachable video/audio/captions, and stops the feed. It
always writes:

- `artifacts/startup-validation/result.json`
- `artifacts/startup-validation/compose-ps.txt`
- `artifacts/startup-validation/docker.log`

Run the marked integration test with:

```sh
VIDEOSIM_STARTUP_INTEGRATION=1 python3 -m unittest tests.test_startup_workflow -v
```

Set `VIDEOSIM_STARTUP_NO_BUILD=1` to reuse previously built images. Override
the isolated endpoints with `VIDEOSIM_STARTUP_HTTP_PORT` and
`VIDEOSIM_STARTUP_FEED_PORT`, and the artifact location with
`VIDEOSIM_STARTUP_ARTIFACT_DIR`.

For a Google Chrome operator check, retain the validated stack and open the GUI:

```sh
VIDEOSIM_STARTUP_KEEP=1 scripts/startup-validation.py
```

Open `http://127.0.0.1:18080` in Chrome. Confirm the startup-validation feed
shows a copyable SRT endpoint and `Validation PASS`, then run Start, Validate,
and Stop once more from the feed detail page. Inspect the same container output
used by the automated gate and tear down the isolated stack:

```sh
docker compose -p videosim-startup-validation logs --no-color app worker
docker compose -p videosim-startup-validation down --volumes --remove-orphans
```

This smoke proves a single normal feed and one worker on the local Docker host.
It does not certify PostgreSQL/NATS production startup, failure recovery, soak
duration, or 1,000-stream capacity.

### Production Compose/VM security boundary

The default Compose file remains a trusted-lab path. For the selected production
Docker Compose/VM target, use the mTLS/OIDC overlay documented in
[`docs/security.md`](docs/security.md):

```sh
cp .env.production.example .env.production
scripts/generate-dev-mtls-certs.sh  # local smoke only; replace in production
# Fill every placeholder in .env.production, including four distinct
# URL-safe PostgreSQL owner/app/publisher/pruner passwords.
docker compose --env-file .env.production \
  -f docker-compose.production.yml config --quiet
# Brand-new deployment only; use the quiesced sequence below for SQLite cutover.
docker compose --env-file .env.production \
  -f docker-compose.production.yml up --build -d
```

The production overlay does not publish app TCP 8080, PostgreSQL 5432, or NATS
4222. Nginx exposes the OIDC operator path on 8443 and the worker-mTLS path on
9443. Worker certificate CN must equal `--worker-id`; the app independently
checks the proxy secret and identity headers. Use a managed CA/secret store and
VM firewall in production.

The overlay also starts single-instance PostgreSQL and NATS JetStream services.
`postgres-role-init` converges the non-owner runtime roles before `migrate`;
`postgres-role-grants` must then complete before `app`, `outbox-publisher`, or
`monitor-history-pruner`. `nats-init` also gates the publisher. Check their
status explicitly:

```sh
docker compose --env-file .env.production -f docker-compose.production.yml ps
docker compose --env-file .env.production -f docker-compose.production.yml \
  logs postgres-role-init migrate postgres-role-grants nats-init outbox-publisher monitor-history-pruner
```

The owner/migration credential is never used by runtime services. The GUI uses
`videosim_app`, the publisher uses `videosim_publisher`, and the pruner uses
`videosim_pruner`; their passwords must be distinct. During an upgrade, quiesce
app/publisher/pruner writes before rerunning this chain because role convergence
intentionally revokes unsafe prior grants/ownership before reapplying the narrow
policy.

For an existing SQLite deployment, do **not** run the full `up` command first.
Quiesce the old app, retain and checksum its SQLite file, then build/start only
storage, migrate, import, verify the idempotent retry, and finally open app/proxy
traffic:

```sh
prod='docker compose --env-file .env.production -f docker-compose.production.yml'
$prod build app
$prod up -d postgres nats
$prod run --rm postgres-role-init
$prod run --rm migrate
$prod run --rm postgres-role-grants
$prod run --rm nats-init
$prod run --rm --no-deps -v "$PWD/data:/import:ro" app \
  python -m videosim import-sqlite-feeds --sqlite-path /import/feeds.sqlite3
# Retry must report zero changed definitions.
$prod run --rm --no-deps -v "$PWD/data:/import:ro" app \
  python -m videosim import-sqlite-feeds --sqlite-path /import/feeds.sqlite3
$prod run --rm migrate python -m videosim migration-status
$prod up -d app outbox-publisher oauth2-proxy proxy worker
```

Compare feed IDs/counts in the retained SQLite snapshot and PostgreSQL-backed
GUI before operator DNS/traffic cutover. On mismatch, stop new services and
restart the retained old image/SQLite file; never run the down migration. The
import excludes transient process and JSON alarm state. PostgreSQL deployments
use worker v2 durable lease/probe-result/catalog-monitor fencing and direct
PostgreSQL monitor reads, but this remains neither an HA nor production-scale
completion claim. See
[`docs/durable-control-plane.md`](docs/durable-control-plane.md) for authority,
rollback, integration evidence, and open gates.

### PostgreSQL backup and restore

Run backups from a host/tool image with PostgreSQL 17 client utilities and the
Python requirements installed:

```sh
export VIDEOSIM_DATABASE_URL='postgresql://.../videosim'
scripts/postgres-backup.sh /secure/backups/videosim.dump
```

Restore into an explicitly approved target, never over the active database:

```sh
export VIDEOSIM_RESTORE_DATABASE_URL='postgresql://.../videosim_restore'
export VIDEOSIM_RESTORE_EXPECTED_TARGET='restore-db.example:5432/videosim_restore'
export VIDEOSIM_RESTORE_CONFIRM="DESTROY_AND_RESTORE $VIDEOSIM_RESTORE_EXPECTED_TARGET"
export VIDEOSIM_RESTORE_BROKER_MODE=empty  # use retained only when that broker survived
scripts/postgres-restore.sh /secure/backups/videosim.dump
scripts/verify-postgres-restore.py \
  --source-url "$VIDEOSIM_DATABASE_URL" \
  --restored-url "$VIDEOSIM_RESTORE_DATABASE_URL"
```

Stop app/scheduler/publisher writes before semantic comparison. Export the same
`POSTGRES_APP_PASSWORD`, `POSTGRES_PUBLISHER_PASSWORD`, and
`POSTGRES_PRUNER_PASSWORD` values used by the target deployment. If the target
URL relies on `.pgpass` rather than inline owner credentials, also export
`POSTGRES_RUNTIME_OWNER_PASSWORD`. Before `pg_restore --clean`, restore performs
only read-only preflight checks: the target credential must be a non-runtime
PostgreSQL owner (or superuser), and all runtime passwords must be distinct from
that owner password and from one another. After migrations, restore converges
runtime-role ownership/membership/grants and
reapplies the narrow policy because `pg_restore --no-privileges` intentionally
omits source ACLs; provision the same roles/passwords on a different target
cluster before opening services. A restore expires restored leases; `empty`
broker mode requeues non-dead outbox events, while
`retained` preserves broker acknowledgement state. Record the DB/broker loss
matrix, backup age, restore duration, semantic comparison, replay boundary, and
any lost event window. All consumers must deduplicate durable event IDs. A
functional local restore does not certify PITR, RPO, or RTO.

Run the separate monitor container beside the GUI:

```sh
docker compose up --build app monitor
```

The monitor polls `http://app:8080/state.json`, validates running feeds, writes
`/tmp/videosim-monitor/state.json`, and repeats active alarm events every 5
seconds until the alarm clears. In SQLite/trusted-lab mode the GUI reads that
shared file. In PostgreSQL worker-v2 mode, reports carry catalog observations
and PostgreSQL atomically owns pending/current alarms, immutable event edges,
and the `/state.json` operator view; it uses server time for alert delay and
repeat cadence. Create or open a feed detail page and use `Alert profile` to
select enabled alarms for that stream, disable alarms, enable all alarms, and
set the alarm delay in seconds. Explicitly disabling an active PostgreSQL alarm
creates a `suppressed` edge rather than a recovery clear. Missing, skipped, or
failed observations never imply a clear. Production Compose runs
`monitor-history-pruner` hourly to delete age-expired durable event rows for
inactive streams too; run an inspected one-shot sweep manually with:

```sh
python3 -m videosim monitor-history-prune --once
```

## Run Master/Worker Monitoring

Use the GUI as the master control plane and start one or more workers that poll
for assignments:

```sh
docker compose up --build app worker
```

The sample worker calls `http://app:8080/api/workers/assignments`, monitors
assigned feeds, and posts reports back to
`http://app:8080/api/workers/report`. Run additional workers by overriding the
worker id:

```sh
docker compose run --rm worker python -m videosim worker \
  --control-plane-url http://app:8080 \
  --worker-id worker-2 \
  --srt-host app
```

For a local worker outside Compose:

```sh
python3 -m videosim worker --control-plane-url http://127.0.0.1:8080 --worker-id local-worker
```

Workers auto-negotiate by storage mode. The default SQLite trusted-lab path uses
`videosim.worker/v1`. PostgreSQL-backed deployments require
`videosim.worker/v2`: one process-incarnation UUID, offered/acknowledged durable
leases, immutable report IDs, per-epoch sequences, and DB-fenced probe plus
catalog-monitor results committed with the direct PostgreSQL alarm projection.
Workers send an independent heartbeat every 20 seconds; in v2 it refreshes
membership and extends only active leases for the same incarnation. Keep the
interval safely below the current 60-second TTL:

```sh
python3 -m videosim worker --control-plane-url http://127.0.0.1:8080 \
  --worker-id local-worker --heartbeat-interval-seconds 15
```

In PostgreSQL mode, `--max-streams N` advertises a static admission limit for
that worker. The scheduler will not assign beyond aggregate advertised
capacity; `capacityShortfall` in the assignment response shows unassigned
running streams. This controls admission only; probe execution is serial by
default and requires separate measured capacity testing.
Use `--max-srt-streams N` and `--max-dash-streams N` for protocol-specific
admission. A worker must satisfy both its total cap and the stream's protocol
cap. Mixed workloads that exceed either cap remain unassigned and increase
`capacityShortfall`. Zero leaves that dimension unbounded; select all limits
from representative SRT/DASH measurements.
Use `--max-concurrent-checks N` to bound simultaneous stream checks. This is a
local execution bound; combine it with the batch budget below to bound queued
probe starts. It is not a deadline or capacity certification.
Use `--max-concurrent-deep-checks N` to cap concurrent TR-101, frame-rate, and
loudness streams independently. Zero inherits `--max-concurrent-checks`; choose
both limits from measured validation/deep-check resource use.
Use `--stream-budget-seconds N` to classify an over-budget stream as timed out
and defer its remaining checks. Built-in GStreamer, FFmpeg, FFprobe, and DASH
polling waits use the remaining budget; this is not cost-tier fairness,
backpressure, or capacity certification.
Use `--deep-check-interval-seconds N` to run TR-101, frame-rate, and loudness
checks once per configured cadence while validation continues each cycle when
the batch budget allows. The worker stores the next due time with retained state
and derives a stable per-stream offset, so subsequent deep checks are spread
across the interval. A deferred deep phase emits one `deep_checks=skipped`
probe metric and no health observation, preserving existing deep-check alarms.
Zero keeps the existing every-cycle deep checks. Select the interval from
measured freshness and worker-capacity evidence; this control is deterministic
load shedding, not dynamic backpressure or capacity certification.
Use `--batch-budget-seconds N` to submit at most one validation or deep window
at a time, using each phase's configured concurrency. Once a completed
window exhausts the aggregate cycle budget, no more validation probes start:
the remaining streams emit inconclusive `validation=skipped` observations,
retain prior alarms, rotate to the front of the next cycle, and the whole deep
phase defers. In-flight checks still run to their per-stream deadline. This
bounds the local executor queue but is not a durable queue, measured capacity,
or protocol/tenant cost fairness. Report delivery pressure is handled by the
encrypted spool below.
Inspect each durable worker's latest pressure snapshot through `/state.json`:

```sh
curl -fsS http://127.0.0.1:8080/state.json | jq '.workers[] | {id, pressure}'
```

`cycleActive` identifies an in-progress cycle; `lastValidationDeferred`,
`lastDeepDeferred`, and `lastBatchDurationMs` describe the previous completed
cycle; `spoolBlocked`, `spoolQueuedReports`, and `spoolBytes` describe local
delivery pressure. `lastBatchCpuMs` is worker plus reaped media-tool CPU for the
completed batch; `processPeakRssBytes` and `childPeakRssBytes` are cumulative
process peaks, and `openFileDescriptors` is sampled on Linux after the batch.
The snapshot advances on heartbeats and has no history or alert policy; retain
external observations when sizing workers or diagnosing saturation/recovery.
In durable PostgreSQL mode, `spoolBlocked=true` removes the worker from
assignment placement. Healthy workers may receive higher-epoch replacement
leases on their next assignment poll; if their advertised limits cannot absorb
the work, `capacityShortfall` remains nonzero. This is immediate load shedding
without hysteresis, a durable queue, or measured recovery/headroom evidence.

For worker API v2, configure all three spool controls together:

```sh
python3 -m videosim worker \
  --report-spool-dir /var/lib/videosim-worker/reports \
  --report-spool-key-file /etc/videosim/certs/worker-spool.key \
  --report-spool-max-bytes 536870912
```

The key file must contain a Fernet key and be mode `0600` or stricter. Each
report is encrypted and fsynced before network delivery, then removed only
after acceptance. On restart, queued reports replay before the new incarnation
registers. HTTP `409` means the old lease is fenced and deletes that stale
entry; retryable transport failures retain it and pause new probes. Quota
exhaustion fails closed. Do not rotate or remove the key while entries remain.
Docker logs emit one `report_spool=blocked` transition and one recovery
transition with aggregate count/byte fields.

On SIGINT or SIGTERM, a worker finishes the current monitor/report cycle, stops
heartbeats, and posts `/api/workers/drain`. PostgreSQL atomically marks that
incarnation and its live leases `draining`; the same incarnation cannot be
reactivated by a late heartbeat, and another worker receives a higher lease
epoch immediately. A hard-killed worker still relies on lease expiry; queued
reports remain encrypted on the worker volume for replay or fenced discard.
With the defaults, reassignment begins on a survivor poll after the 60-second
database freshness/lease boundary. The integration suite exercises the same
path with a one-second test TTL; production partition timing remains an
environment gate:

```sh
VIDEOSIM_TEST_POSTGRES_URL=... python3 -m unittest \
  tests.test_worker_v2.DurableWorkerV2ApiIntegrationTest.test_hard_kill_reassigns_only_after_database_ttl_expiry
```

Strict versioned reports are the default. During a controlled same-host upgrade,
the GUI can temporarily accept old unversioned reporters with
`--allow-legacy-worker-reports`; this mode still scopes reports to current
ownership but cannot fence stale generations and must not be used as a
production setting.

Generated DASH assignments include a master-served manifest URL. Generated SRT
assignments use the worker's `--srt-host` value to reach listener feeds.

Run the deterministic control-plane-only foundation benchmark:

```sh
python3 -m videosim control-plane-benchmark \
  --streams 1000 --workers 10 --iterations 3 --warmup-iterations 1 --seed 17 --json
```

The report must say `scope=in_process_control_plane_only`,
`mediaProbesExecuted=false`, and `capacityCertified=false`. It proves assignment
coverage, unique ownership, contract metadata, and report acceptance in one
process. It does not run SRT/DASH media checks or satisfy a scale-admission gate.

To inject one process-local worker loss against target plus 30% logical
headroom:

```sh
python3 -m videosim control-plane-benchmark \
  --streams 1300 --workers 10 --fail-workers 1 \
  --iterations 3 --warmup-iterations 1 --seed 17 --json
```

Require complete assignments across the nine survivors and one
`staleReportsRejected`. Compare `reassignedStreams` with
`minimumReassignments`; `excessReassignments` is avoidable placement churn. This
uses process-local worker removal and does not exercise PostgreSQL leases, TTL,
network partitions, or infrastructure failure domains.

Measure the real worker probe path against a captured state scenario:

For the deterministic DASH matrix, start the blocking fixture service in one
terminal:

```sh
python3 -m videosim fixture-fleet \
  --manifest scale/fixtures/dash-matrix.json \
  --state-path artifacts/dash-fixtures/state.json
```

The checked-in manifest advertises `127.0.0.1`. When the benchmark runs in a
different container on the same network, add `--advertised-host <fixture DNS
name>`. The service launches the existing healthy DASH generator, serves its
manifest/segments, delays every slow response for 30 seconds, returns `503` for
dead, and serves malformed MPD bytes for malformed. Stop it with Ctrl-C.

For SRT, run the companion matrix:

```sh
python3 -m videosim fixture-fleet \
  --manifest scale/fixtures/srt-matrix.json \
  --state-path artifacts/srt-fixtures/state.json
```

It reserves four consecutive ports from `basePort`: a normal MPEG-TS feed, a
listener that stalls each payload for 30 seconds, an unbound dead port, and a
listener emitting paced non-MPEG-TS bytes. Use the same `--advertised-host`
override across containers. The process fails if any launched listener exits
and stops all child pipelines on Ctrl-C.

With both fleet processes running, compose the checked-in mixed scenario:

```sh
python3 -m videosim fixture-scenario \
  --manifest scale/fixtures/mixed-1000.json \
  --srt-state artifacts/srt-fixtures/state.json \
  --dash-state artifacts/dash-fixtures/state.json \
  --state-path artifacts/mixed-fixtures/state.json
```

The composer validates and hashes both fixture states, allocates exact protocol
and behavior percentages, gives every logical stream a unique ID, and performs
a seeded shuffle. The 1,000 streams share eight endpoints; use this only to test
bounded scheduling/deferral and never as media-capacity evidence.

For 1,000 distinct protocol URLs, run the fleets with
`scale/fixtures/srt-endpoints-500.json` and
`scale/fixtures/dash-endpoints-500.json`, then pass those two states to the same
composer command. The SRT manifest uses one caption-capable encoder, one local
multicast distributor, 400 lightweight healthy relays, 50 slow processes, and
25 malformed processes while leaving 25 ports dead by design. Use a
resource-controlled Linux load host. The 500 DASH paths share one generator and
HTTP origin. Distinct URLs prove request/socket fan-out only; they do not prove
independent source generation or capacity.

For a 32%-headroom candidate, substitute
`scale/fixtures/srt-endpoints-660.json`,
`scale/fixtures/dash-endpoints-660.json`, and
`scale/fixtures/mixed-1320.json`. They compose 1,320 distinct URLs with an
exact 660/660 protocol and 1,056/132/66/66 behavior mix. Split the composed
state into ten 132-stream worker scenarios with 66 SRT and 66 DASH assignments
each before running workers concurrently. This is a saturation input only. The
recorded five-second-budget run covered every URL at least twice but failed the
90-second gap gate on two workers (worst upper bound 91.473 seconds) and must
not be used as F5 evidence.

A calibrated follow-up used 22 balanced 60-stream survivors, 12 validation
tokens each, and the measured 15-second SRT budget. Fourteen workers still
failed the 90-second gate; the worst upper bound was 99.052 seconds. The
single Docker host reached 5.31 GiB and 7,386 processes/threads during load,
with about 954% aggregate worker CPU before fixture CPU. Do not raise local
concurrency further; distribute this workload across real failure domains and
rerun the same strict reports.

Use `scale/workloads/f5-1000-candidate.json` for that distributed run. Place 11
of its 33 workers in each of three independent failure domains. Each worker has
60 total, 30 SRT, and 30 DASH admission tokens; after losing any one domain,
the 22 survivors retain exact 1,320/660/660 total/SRT/DASH capacity. Run the
declared domain-loss and endpoint-fault storms and retain every required
artifact for `capacity-check`. If measured hosts cannot sustain this shape,
revise the candidate before rerunning rather than weakening the admission gate.

### Boot the candidate fixture domains

Run `docker-compose.fixture-domain.yml` on three separate Linux load hosts,
not on the worker-domain hosts. On each host, copy
`.env.fixture-domain.example` to `.env.fixture-domain`, set the same
immutable image digest, set a unique worker-reachable advertised hostname, and
create the configured state directory. The Compose file uses Linux host
networking so its SRT listeners and DASH origin bind directly on that host.

Boot and validate each domain:

```sh
set -a
. ./.env.fixture-domain
set +a
VIDEOSIM_FIXTURE_STARTUP_ARTIFACT_DIR="$VIDEOSIM_FIXTURE_STATE_DIR/startup-validation" \
  python3 scripts/fixture-domain-startup.py
```

The command fails unless the image uses an immutable digest, both services
remain running, the DASH `/healthz` API passes, one healthy SRT and one healthy
DASH endpoint pass full media validation, and Docker logs contain no critical
marker. Retain `result.json`, `media-validation.json`, `compose-ps.txt`,
`docker.log`, `srt-state.json`, and `dash-state.json` from every host.

Copy the six state files to the workload coordinator and compose the exact
candidate input:

```sh
python3 -m videosim fixture-scenario \
  --manifest scale/fixtures/mixed-1320.json \
  --srt-state artifacts/fixture-a/srt-state.json \
  --srt-state artifacts/fixture-b/srt-state.json \
  --srt-state artifacts/fixture-c/srt-state.json \
  --dash-state artifacts/fixture-a/dash-state.json \
  --dash-state artifacts/fixture-b/dash-state.json \
  --dash-state artifacts/fixture-c/dash-state.json \
  --state-path artifacts/mixed-1320.json
```

Require `fixtureStateCounts={"srt":3,"dash":3}`,
`logicalStreamsShareEndpoints=false`, 660 streams per protocol, and 1,320
unique endpoint URLs. The startup check samples two healthy endpoints; it does
not prove that a 220+220 domain stays healthy under concurrent load. Each SRT
shard still shares one encoder and each DASH shard one generator/origin, so
this topology reduces shared fate without proving per-stream source
independence.

### Boot the candidate worker domains

Run `docker-compose.worker-domain.yml` on three separate Linux hosts. On each
host, copy `.env.worker-domain.example` to `.env.worker-domain`, replace the
image placeholder with the same immutable digest, and set
`VIDEOSIM_FAILURE_DOMAIN` to one of the three zones in the workload manifest.
Provision `server-ca.crt`, `worker-spool.key`, and 11 worker certificate/key
pairs whose CN and filename stem exactly match that host's worker IDs. Create
the configured data directory before startup.

Render, boot, and capture the domain state:

```sh
set -a
. ./.env.worker-domain
set +a
mkdir -p artifacts
docker compose --env-file .env.worker-domain \
  -f docker-compose.worker-domain.yml config --quiet
docker compose --env-file .env.worker-domain \
  -f docker-compose.worker-domain.yml pull
docker compose --env-file .env.worker-domain \
  -f docker-compose.worker-domain.yml up -d
docker compose --env-file .env.worker-domain \
  -f docker-compose.worker-domain.yml ps > artifacts/worker-domain-ps.txt
curl --fail --silent --show-error \
  --cacert "$VIDEOSIM_WORKER_CERT_DIR/server-ca.crt" \
  "$VIDEOSIM_CONTROL_PLANE_HEALTH_URL" > artifacts/control-plane-health.txt
docker compose --env-file .env.worker-domain \
  -f docker-compose.worker-domain.yml logs --no-color \
  > artifacts/worker-domain-docker.log
```

Repeat on all three hosts. In an OIDC-authenticated Chrome session, open the
operator `/state.json` endpoint and verify 33 unique candidate worker IDs,
`pressure.assignedStreams=40` for every worker after convergence, and no
blocked spool. Retain that response. Treat a restarting container, worker API
authentication error, assignment shortfall, traceback, or fatal Docker-log
entry as a failed startup.

From a coordinator with read access to the same PostgreSQL database, set
`VIDEOSIM_DATABASE_URL` and retain the converged authority baseline:

```sh
python3 -m videosim verify-assignments \
  --workload scale/workloads/f5-1000-candidate.json \
  --output artifacts/assignments-baseline.json
```

Require `passed=true`, `phase=baseline`, 33 fresh workers, 1,320 authoritative
assignments, 660 assignments per protocol, and exactly 40/20/20 total/SRT/DASH
assignments per worker. The capture uses one read-only repeatable PostgreSQL
snapshot and treats only an active, unexpired, current-config lease held by the
fresh matching worker incarnation as authoritative.

### Run durable control-plane load

Provision a separate empty PostgreSQL database with the same version and
settings as the candidate, apply migrations, and run:

```sh
python3 -m videosim control-plane-load \
  --database-url "$VIDEOSIM_LOAD_DATABASE_URL" \
  --workload scale/workloads/f5-1000-candidate.json \
  --duration 24h --tick-seconds 20 \
  --worker-freshness-seconds 60 \
  --output artifacts/control-plane-load.json
```

The preflight rejects any existing non-default tenant, feed, worker, report,
audit, inbox, or outbox data. On the F5 candidate, initial assignment must be
exactly 33 workers at 40 total/20 SRT/20 DASH streams each. Every tick refreshes
all worker heartbeats; workload check profiles schedule fenced synthetic probe
results at their declared cadences. Require zero rejected/duplicate results,
one accepted-result outbox event per accepted report, 1,320 current checked
streams and authoritative leases, and non-empty heartbeat/report/tick latency
percentiles.

After at least one baseline report, the declared worker-domain-loss event stops
heartbeats and reports for the first workload domain. The harness waits for
PostgreSQL freshness expiry, invokes the production capacity-aware scheduler,
acknowledges replacement leases, attempts one stale failed-owner report, and
forces a survivor report window. Require `status=recovered`, 11 failed and 22
surviving workers, 440 affected and changed owners, zero healthy-domain owner
changes, `staleReportAccepted=false` with an explicit rejection reason, full
survivor report coverage, and authority-recovery p95/p99 at or below 45/90
seconds. A run that reaches the event but cannot recover before its duration
fails. A shorter run records `not_reached` and proves no failure behavior.

`--worker-freshness-seconds` must equal the candidate deployment's actual
freshness setting. The current control-plane default is 60 seconds, which
cannot satisfy the 45-second p95 gate; do not lower only the harness value to
manufacture a pass. The P0 integration test uses one second solely to compress
wall-clock test time.

The command intentionally retains the synthetic tenant, worker reports, check
results, current state, leases, and immutable outbox rows. Retain a database
snapshot with the JSON report, then destroy the disposable database as a whole;
do not selectively delete immutable evidence. `mediaProbesExecuted=false` and
`capacityCertified=false` are permanent report fields. This harness measures
the PostgreSQL control-plane path only and cannot replace worker HTTP/mTLS,
broker-consumer, independent media, physical failure-domain, or 24-hour
admission evidence. Declared endpoint-fault storms remain `out_of_scope`
because they require real media probes.

After the declared endpoint-fault storm has raised and cleared alarms, retain a
durable projection report:

```sh
python3 -m videosim verify-alarm-consistency \
  --workload scale/workloads/f5-1000-candidate.json \
  --output artifacts/alarm-consistency.json
```

Require `passed=true`, `expectedDesiredStreams=1320`,
`desiredStreams=1320`, `observedDesiredStreams=1320`, at least one retained
alarm event, and zero violations in every named check. The command uses one
read-only repeatable PostgreSQL snapshot. It reconciles each current check with
its source and latest result; pending/current alarms with conclusive source
results; the latest retained transition with current alarm state; and every
retained event with its identity payload and transactional outbox record. An
inconclusive current observation may preserve an older active alarm source, but
an inactive alarm without a matching retained `cleared` or `suppressed` edge
fails closed. Retain the JSON report with the workload, database metrics, and
Docker logs.

Run the same command after recovery and at the end of the soak. A passing report
does not measure transition latency, broker/consumer delivery, retention beyond
the captured window, independent-host behavior, or 24-hour stability; those
remain separate admission evidence.

At the declared domain-loss offset, stop all 11 services on one host. After the
lease TTL and assignment-poll bound, the authenticated state must contain 22
fresh workers at 60 assignments each. The immutable assignment capture must
prove 30 SRT and 30 DASH leases per survivor, no duplicate authority, and only
the failed domain's 440 ownership changes:

```sh
python3 -m videosim verify-assignments \
  --workload scale/workloads/f5-1000-candidate.json \
  --baseline artifacts/assignments-baseline.json \
  --output artifacts/assignments-domain-loss.json
```

Require `passed=true`, `phase=1-domain-loss`, 22 fresh workers,
`ownershipChanges=440`, and `expectedOwnershipChanges=440`. Restart the domain
and rerun with the same baseline into `assignments-recovery.json`; require the
baseline shape and zero ownership changes. Retain all three reports with the
before/loss/recovery API, database, process, metric, and Docker-log artifacts,
then continue the 24-hour run. This verifier does not prove stale-report
rejection, failover latency, probe freshness, or media health; retain those
separate chaos/API/worker evidence. A same-host Compose test or a successful
config render is not failure-domain or capacity evidence.

For a strict rotation run with eight admitted validations per cycle:

```sh
python3 -m videosim worker-benchmark \
  --scenario artifacts/mixed-fixtures/state.json \
  --iterations 250 --warmup-iterations 0 \
  --max-concurrent-checks 8 --max-concurrent-deep-checks 2 \
  --stream-budget-seconds 0.03 --batch-budget-seconds 0.001 \
  --require-full-validation-coverage \
  --max-validation-gap-cycles 125 \
  --max-validation-gap-seconds 90 --json
```

Require `validationAttemptedStreams=1000`, `validationCoveragePercent=100`,
`cyclesToFullValidationCoverage<=125`,
`timeToFullValidationCoverageSecondsUpperBound<=90`,
`minimumValidationAttempts>=2`, `maximumValidationGapCycles<=125`, and
`maximumValidationGapSecondsUpperBound<=90`. The gaps include the initial and
trailing measured windows, so early attempts followed by starvation fail. The
seconds value conservatively spans cycle boundaries around each validation
start; it is not an exact per-probe timestamp. This proves bounded cursor
service for the supplied logical assignments under this test threshold, not an
approved freshness SLO, production probe budgets, or independent media
capacity.

Inspect `results.validationOutcomesByProtocol` before interpreting a passing
rotation gate. The scaled SRT relay validates a fresh connection quickly but
needs about 10 seconds for repeated full media validation; a five-second stream
budget therefore records SRT timeouts even when DASH remains healthy. A local
16-URL calibration produced 8 SRT successes/8 timeouts at five seconds and 16
SRT successes at 15 seconds. Select a measured protocol budget before using a
run as quality evidence.

To benchmark an existing app instead, capture its state:

```sh
mkdir -p artifacts/worker-benchmark
curl -fsS http://127.0.0.1:8080/state.json \
  > artifacts/worker-benchmark/state.json
python3 -m videosim worker-benchmark \
  --scenario artifacts/worker-benchmark/state.json \
  --iterations 3 --warmup-iterations 1 \
  --max-concurrent-checks 8 --max-concurrent-deep-checks 2 \
  --stream-budget-seconds 30 --batch-budget-seconds 300 --json \
  > artifacts/worker-benchmark/report.json
```

The scenario uses the existing GUI `/state.json` shape and must contain unique
running stream IDs with endpoints reachable from the benchmark host. The report
hashes that scenario and records real probe cycle/CPU percentiles, aggregate
and per-protocol validation outcomes, worker/child peak RSS, and post-cycle
Linux descriptors. A passing report means
every running stream produced probe metrics in every measured cycle; outage
outcomes remain measurements. Run separate scenarios at increasing per-worker
stream counts to build a saturation curve.

This is a single-worker measurement. It does not prove fleet headroom, lease
correctness, HA/failover, or 24-hour stability.

### Scale evidence admission

After a production-like run has produced its immutable bundle, verify it against
the versioned F5 policy:

```sh
python3 -m videosim capacity-check \
  --report artifacts/scale/evidence.json \
  --policy scale/policies/f5-1000.json --json
```

Artifact paths must be relative to the evidence report and every declared
SHA-256 must match. The workload must declare the protocol/source mix,
region/zone placement, check cadence, endpoint and event-storm distributions,
worker/failure-domain shape, infrastructure versions, 24-hour duration, 1,000
target streams, at least 1,300 load streams, and one unavailable failure domain.
The checked-in candidate declares 1,320 load streams. Survivor total/SRT/DASH
tokens must carry the declared protocol load. The report must have a clean
source commit, immutable image digests, all required raw artifacts, no skipped
checks, and passing detail for all ten admission criteria.

Exit zero proves only that a complete, untampered bundle satisfies the policy.
The repository does not yet contain the production-like run or evidence needed
for 1,000-stream admission.

The monitor also samples MPEG-2 TS bytes and raises TR 101 290 priority 1/2 TS
alarms plus parser-backed priority 3 PSI/SI, unreferenced-PID, and T-STD timing
alarms when checks fail. Video-present feeds get an FFprobe frame-rate check
against the configured feed rate, raising a `Video frame rate match` alarm when
the measured rate differs by more than 0.15 fps. Audio-present feeds also get
FFmpeg `ebur128` loudness checks for ITU-R BS.1770 measurement availability,
EBU R 128 integrated loudness/true peak, and ATSC A/85 integrated loudness.

Run the Docker monitor fixture gate:

```sh
docker compose run --build --rm monitor-fixtures
```

Verbose logging is enabled by default for the Compose app. Watch feed creation,
container status, subprocess PID, and the exact GStreamer pipeline:

```sh
docker compose logs -f app
```

Print the GStreamer command without starting a feed:

```sh
python3 -m videosim start --port 9000 --print-command
```

Stop a running feed with Ctrl-C.

## Planned Receiver Commands

```sh
ffplay "srt://127.0.0.1:9000?mode=caller"
ffprobe -hide_banner "srt://127.0.0.1:9000?mode=caller"
gst-launch-1.0 srtsrc uri="srt://127.0.0.1:9000?mode=caller" ! tsdemux ! fakesink
ffprobe -hide_banner "http://127.0.0.1:8080/dash/manifest.mpd"
```

Receiver compatibility evidence is maintained in
[docs/compatibility-report.md](docs/compatibility-report.md).

## Troubleshooting

- Missing `gst-launch-1.0`: rerun `scripts/install-deps.sh` or install
  GStreamer tools manually.
- Missing `srtsink` or `srtsrc`: install the GStreamer bad plugins package for
  the platform.
- Missing `ffprobe`: install FFmpeg.
- FFmpeg reports `Protocol not found` for SRT: use GStreamer tools or install an
  FFmpeg build with SRT enabled.
