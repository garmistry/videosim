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

The React GUI polls `/state.json` every second and shows per-feed estimated bit
rate, outbound total data, uptime, and generated video frame count. These are
runtime estimates derived from the configured media tracks and elapsed time;
feed detail pages keep a rolling five-minute in-browser window and plot bit
rate plus outbound data in real time. Use Validate to prove actual
receiver-visible stream state.

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
Use `--max-concurrent-checks N` to bound simultaneous stream checks. This is a
local execution bound, not a deadline, backpressure, or capacity certification.

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
