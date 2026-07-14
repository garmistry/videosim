# Video Feed Simulator

Video Feed Simulator generates local live video feeds for testing receivers,
monitoring systems, outage handling, and alert workflows. It provides a browser
GUI, command-line tools, SRT/DASH feed generation, validation, and optional
distributed monitoring workers.

## Features

- Generate SRT and DASH feeds from synthetic media.
- Run normal video/audio/caption feeds and fault modes: audio only, video only,
  no captions, black video, and frozen video.
- Register external SRT or DASH feeds for validation and alerting.
- Start, stop, validate, and inspect feeds from the GUI.
- Copy receiver endpoints and view feed logs, errors, preview frames, metrics,
  alarms, and event history.
- Persist feed definitions and alert profiles in SQLite by default; the
  production overlay uses a PostgreSQL/JetStream durable foundation with
  selective durable security auditing and non-owner service roles.
- Run local monitoring or master/worker monitoring; PostgreSQL deployments use
  durable worker-v2 lease/report fencing plus direct PostgreSQL monitor
  alarm/event reads, while SQLite retains lab-compatible v1/file JSON.

## Quick Start

Install system dependencies:

```sh
scripts/install-deps.sh
```

Start the GUI with Docker Compose:

```sh
docker compose up --build app
```

Open:

```text
http://127.0.0.1:8080
```

Start the GUI with the monitor:

```sh
docker compose up --build app monitor
```

Start the GUI with a worker node polling the master control plane:

```sh
docker compose up --build app worker
```

Run the isolated startup/API validation workflow:

```sh
scripts/startup-validation.py
```

It boots the Compose app and worker, creates and starts a normal SRT feed
through HTTP, proves receiver-visible video/audio/captions, stops the feed, and
writes Docker logs plus JSON evidence under `artifacts/startup-validation/`.
This is a one-feed startup gate, not 1,000-stream capacity evidence.

For PostgreSQL worker admission, pass `--max-streams N` to advertise a static
per-worker limit. Streams over aggregate advertised capacity remain unassigned
and are reported as a capacity shortfall; this is not media-capacity evidence.
Use `--max-srt-streams N` and `--max-dash-streams N` to refine that admission
limit by protocol. The durable scheduler enforces total and matching protocol
caps together; zero omits a cap. Set counts from measured worker evidence.
Use `--max-concurrent-checks N` to bound simultaneous stream checks on a worker;
checks within each stream remain ordered.
Use `--max-concurrent-deep-checks N` to cap TR-101, frame-rate, and loudness
streams separately; zero inherits `--max-concurrent-checks`.
Use `--stream-budget-seconds N` to stop starting lower-priority checks after a
stream exhausts its budget. Built-in media subprocess and DASH polling waits
are capped by the remaining budget.
Use `--deep-check-interval-seconds N` to stagger TR-101, frame-rate, and loudness
analysis while validation continues each cycle when the batch budget allows.
Zero retains every-cycle deep checks.
Use `--batch-budget-seconds N` to submit validation in
`--max-concurrent-checks`-sized windows and deep work in its separately bounded
windows. After the budget is consumed, the worker stops starting validation,
records deferred streams as inconclusive, rotates them to the front of the next
cycle, and skips the deep phase. This bounds the local executor queue, not media
capacity or a durable fleet queue.
Long-running workers publish a bounded `capacity.pressure` snapshot on their
independent heartbeat. PostgreSQL `/state.json` exposes it as
`workers[].pressure`, including active-cycle, deferral, batch-duration, and
spool-occupancy fields. Completed batches also publish aggregate CPU time,
worker/child peak RSS, and Linux open-file-descriptor count. This is current
state for sizing runs, not historical capacity evidence.
PostgreSQL deployments use the versioned operator read model for the GUI:
`GET /api/operator/feeds` pages the shared catalog, `GET
/api/operator/overview` returns bounded monitor summary data, and `GET
/api/operator/feeds/<feed-id>` returns one feed plus its scoped monitor state.
Catalog pages default to 100 and cap at 200 rows. A durable external feed that
is not current in the serving process remains configuration-writable while its
runtime state is marked unknown; a generated feed remains read-only there.
PostgreSQL-backed API processes do not preload the feed catalog at startup;
durable creates use `stream-<32-hex-characters>` IDs and the existing
version-zero insert fence rejects a collision instead of overwriting a row.
Durable update, alert-profile, expectation, and delete forms require the
displayed `config_version`. External-feed update/alert/delete requests may hit
any replica; stale versions return HTTP 409 without mutation. Generated runtime
ownership and runtime actions are not replica-safe yet.
When a durable worker reports `spoolBlocked=true`, assignment excludes that
worker and reports any resulting `capacityShortfall` instead of granting it new
lease authority. This is fail-closed load shedding, not a durable fleet queue.
Worker API v2 can enable an authenticated-encrypted write-ahead report spool
with `--report-spool-dir`, `--report-spool-key-file`, and
`--report-spool-max-bytes`. All three are required. Pending reports replay
before a new incarnation registers, and new probes pause while delivery is
blocked. The production Compose overlay enables a persistent 512 MiB spool.
With concurrent checks enabled, admitted validation windows finish before the
worker starts TR-101, frame-rate, or loudness checks.

Run a feed directly from the CLI:

```sh
python3 -m videosim start --profile profiles/srt-normal.yaml --port 9000
```

Validate a running feed:

```sh
python3 -m videosim validate --profile profiles/srt-normal.yaml --port 9000
```

Receiver URL:

```text
srt://127.0.0.1:9000?mode=caller
```

## Supported Modes

| Mode | Video | Audio | Captions |
|---|---|---|---|
| `normal` | moving/generated | present | present |
| `audio_only` | absent | present | absent/unsupported |
| `video_only` | moving/generated | absent | present |
| `no_captions` | moving/generated | present | absent |
| `black_video` | black frames | present | present |
| `frozen_video` | static/repeated frames | present | present |

## Verification

Run the local test suite:

```sh
python3 -m unittest discover -s tests
```

Run the Linux/Docker gates:

```sh
docker compose run --build --rm test
docker compose run --build --rm live-srt
docker compose run --build --rm monitor-fixtures
VIDEOSIM_STARTUP_INTEGRATION=1 python3 -m unittest tests.test_startup_workflow
```

The live SRT tests are skipped locally unless `VIDEOSIM_LIVE_SRT=1` is set.
PostgreSQL/JetStream integration tests are separately gated by
`VIDEOSIM_TEST_POSTGRES_URL` and `VIDEOSIM_TEST_NATS_URL`; optional runtime-role
coverage also uses the three `VIDEOSIM_TEST_POSTGRES_*_URL` service URLs. See the
durable control-plane document. Passing either suite is not a scale-admission
result.

Exercise the versioned in-process worker assignment/report invariants:

```sh
python3 -m videosim control-plane-benchmark --streams 1000 --workers 10 --iterations 3
```

This command runs no media probes and is not evidence of 1,000-stream monitoring capacity.
Add `--fail-workers 1` to remove one process-local worker after warmup, require
complete unique survivor coverage, and prove that its stale report is rejected.
Use `--streams 1300` to model 30% logical headroom; reassignment counters expose
placement churn but still provide no durable or media-capacity evidence.

Measure one worker against live endpoints from an exported GUI state:

```sh
python3 -m videosim fixture-fleet \
  --manifest scale/fixtures/dash-matrix.json \
  --state-path artifacts/dash-fixtures/state.json
```

For the equivalent SRT transport matrix, use a separate terminal and state:

```sh
python3 -m videosim fixture-fleet \
  --manifest scale/fixtures/srt-matrix.json \
  --state-path artifacts/srt-fixtures/state.json
```

Each blocking fixture service exposes healthy, 30-second slow, unavailable,
and malformed endpoints. Run the worker benchmark in a second terminal against
the selected generated state file.

After starting both protocol fleets, compose the deterministic 1,000-logical-
stream scenario:

```sh
python3 -m videosim fixture-scenario \
  --manifest scale/fixtures/mixed-1000.json \
  --srt-state artifacts/srt-fixtures/state.json \
  --dash-state artifacts/dash-fixtures/state.json \
  --state-path artifacts/mixed-fixtures/state.json
```

The output has exact 50/50 protocol and 80/10/5/5 healthy/slow/dead/malformed
counts, but reuses the eight physical fixture endpoints. It is a bounded-worker
stress input, not independent-stream or capacity evidence.

`scale/fixtures/srt-endpoints-500.json` and
`scale/fixtures/dash-endpoints-500.json` instead declare 500 distinct protocol
URLs each with that same behavior mix. The SRT fleet encodes one captioned
transport stream, distributes it over local multicast, and uses a lightweight
relay for each healthy listener; slow and malformed listeners remain separate
fault processes. The DASH URLs still share one generator/origin. These are load
inputs, not capacity evidence, until the complete F5 gate passes.

The corresponding `*-endpoints-660.json` manifests and `mixed-1320.json`
provide a deterministic 1,320-URL input, or 32% logical headroom over the F5
1,000-stream target. They are a reproducible saturation/regression workload,
not admitted capacity: the recorded ten-worker run failed the 90-second
freshness gate on two shards, and healthy URLs still share protocol sources.

To separate fixture generation from worker hosts, run
`docker-compose.fixture-domain.yml` on three Linux load hosts. Each host uses
the checked-in 220-SRT/220-DASH manifests and a unique advertised hostname.
`scripts/fixture-domain-startup.py` renders and boots the domain, checks the
DASH health API, fails unless every manifest endpoint appears exactly once in
the generated state, validates one healthy SRT and DASH endpoint through the
real media-probe path, and captures a bounded Docker Engine identity, Compose
state, Docker resources, and logs.
`scripts/fixture-domain-fault.py` then stops both source services, requires the
sampled healthy SRT/DASH paths to become unreachable, restarts the same
containers, and retains timed recovery evidence while leaving the domain up.
Pass each retained state to `fixture-scenario` with repeated `--srt-state` and
`--dash-state` options; duplicate endpoints across shards fail closed.
Three shards compose the exact 660/660 candidate input, but each shard still
shares one source per protocol and startup validation is not saturation
evidence. The fault workflow samples two paths; durable worker alarms must
separately prove every affected endpoint transition.

Run the fail-closed local durable startup gate with one immutable application
image:

```sh
VIDEOSIM_DURABLE_FIXTURE_IMAGE='videosim@sha256:<digest>' \
  python3 scripts/durable-fixture-startup.py
```

It boots the 220-SRT/220-DASH fixture shard, a fresh PostgreSQL database, the
API, and 11 real workers; imports `mixed-440-domain.json`; requires exact
balanced leases, behavior-level media outcomes, and malformed-DASH alarms;
pages the operator API; and captures Docker state, resources, and logs under
`artifacts/durable-fixture-startup/`. The API is the default validation surface.
Set `VIDEOSIM_DURABLE_FIXTURE_KEEP=1` to retain the stack for a Chrome check.
This same-host diagnostic permanently reports `capacityCertified=false`; F5
still requires the documented three-host 1,320-stream run and 24-hour bundle.

Load a distinct-endpoint scenario into a dedicated migrated PostgreSQL catalog
before booting candidate workers:

```sh
python3 -m videosim import-fixture-scenario \
  --state artifacts/mixed-1320.json \
  --output artifacts/fixture-catalog-import.json
```

The command validates the composed state, maps every fixture ID and endpoint to
the existing durable external-feed contract, rejects any unlisted catalog row,
imports all rows in one transaction, verifies the persisted catalog, and writes
an idempotent evidence report. Shared endpoints fail by default. This proves
catalog identity and assignment input only; the report permanently leaves
media-capacity and all-endpoint validation certification false.

To fail unless cursor rotation starts validation for every logical stream, add
`--require-full-validation-coverage` and run enough measured iterations. Add
`--max-validation-gap-cycles` and `--max-validation-gap-seconds` to require at
least two starts per stream and bound the initial, repeat, and trailing service
gaps. The wall-time value is a conservative cycle-boundary upper bound, not a
per-probe timestamp. With eight validation tokens, the checked-in 1,000-stream
scenario requires at least 125 cycles when each aggregate budget admits one
window.

```sh
python3 -m videosim worker-benchmark \
  --scenario artifacts/dash-fixtures/state.json \
  --iterations 3 --warmup-iterations 1 \
  --max-concurrent-checks 8 --max-concurrent-deep-checks 2 --json
```

This runs real media probes and reports latency, CPU, peak RSS, descriptors,
aggregate outcomes, per-protocol validation outcomes, and unique validation
coverage. It measures only the supplied worker scenario and does not certify
fleet capacity.

Exercise the durable PostgreSQL lease, heartbeat, fenced-result, current-state,
and transactional-outbox paths with the checked-in F5 workload:

```sh
python3 -m videosim control-plane-load \
  --database-url "$VIDEOSIM_LOAD_DATABASE_URL" \
  --workload scale/workloads/f5-1000-candidate.json \
  --duration 24h --worker-freshness-seconds 30 \
  --output artifacts/control-plane-load.json
```

The target must be an empty disposable database. The command retains its
synthetic tenant, results, and immutable outbox evidence for inspection, so
destroy the database as a whole after capture. It schedules synthetic probe
profiles and executes the workload's single worker-domain-loss event through
real PostgreSQL expiry, the production scheduler, lease fencing, and survivor
reports. The freshness value must match the deployed control plane. The current
30-second default with five-second worker heartbeats recovered the exact
1,320-stream control-plane shape at 29.888-second p95 in retained PostgreSQL
evidence. At the declared endpoint-fault offset it also commits separate
synthetic `feed_reachable` unhealthy and healthy report windows, requiring
exact raised, cleared, and alarm-outbox counts for all 1,320 streams. The
command runs no media probes and always reports `capacityCertified=false`;
neither synthetic event is physical-domain or media-capacity admission
evidence.

Verify a completed scale-evidence bundle against the fail-closed F5 policy:

```sh
python3 -m videosim capacity-check \
  --report artifacts/scale/evidence.json \
  --policy scale/policies/f5-1000.json
```

The verifier checks workload/headroom/failure-domain declarations, all ten
admission criteria, run duration, skipped checks, artifact paths and SHA-256
hashes. It does not generate the required 24-hour production-like evidence.

The current multi-host run candidate is
`scale/workloads/f5-1000-candidate.json`: 33 workers split evenly across three
failure domains. Losing one domain leaves 22 workers with exact capacity for
1,320 streams, including 660 SRT and 660 DASH. This is checked configuration,
not an approved capacity result.

After collecting startup artifacts from all three fixture hosts and all three
worker hosts, run `python3 scripts/f5-domain-preflight.py` with the three
ordered `--fixture-result`/`--srt-state`/`--dash-state` triplets and three
`--worker-result` files. It rejects incomplete domains, reused advertised
hosts/endpoints, reused Docker Engine or worker-incarnation IDs, worker ID or
zone drift, mutable/mismatched images, and tampered state hashes. A passing
report requires `dockerHostCount=6`, `distinctDockerHostsValidated=true`, and
exact 33-worker registration/incarnation counts. Docker daemon IDs do not
attest physical topology, so the report always keeps
`independentHostsCertified` and `capacityCertified` false; see `RUNBOOK.md` for
the full command.

After an endpoint-fault exercise, reconcile the durable result and alarm
projections from one PostgreSQL snapshot:

```sh
python3 -m videosim verify-alarm-consistency \
  --workload scale/workloads/f5-1000-candidate.json \
  --output artifacts/alarm-consistency.json
```

The command requires all 1,320 desired streams to have current check state,
requires retained alarm transitions, and fails on result/current-state drift,
an unexplained clear, malformed alarm events, or a missing/mismatched outbox
record. Use `--tenant-id` with the `tenantId` emitted by `control-plane-load`
when inspecting its disposable database. It is a consistency gate, not the
missing multi-host 24-hour run.

Boot and validate one 11-worker domain with:

```sh
cp .env.worker-domain.example .env.worker-domain
set -a
. ./.env.worker-domain
set +a
python3 scripts/worker-domain-startup.py
```

Use the same file on three separate hosts with distinct failure-domain names,
worker certificates, and spool storage. The command validates the candidate
zone and all 11 certificate identities, immutable image resolution, host and
worker-path health APIs, a bounded Docker Engine identity, process stability,
all 11 accepted worker registration records, and Docker logs, then retains the
evidence directory without stopping the workers. The runbook contains the
cross-domain Chrome/API, failure-injection, and admission checks.

## Documentation

- [Runbook](RUNBOOK.md): install, run, verify, operate, and troubleshoot.
- [Implementation notes](docs/implementation.md): architecture, persistence,
  GUI behavior, and internal runtime details.
- [Monitoring](docs/monitoring.md): alert profiles, alarm lifecycle, TR 101 290,
  frame-rate checks, and loudness checks.
- [Distributed architecture](docs/distributed-architecture.md): implemented
  master/worker slice and current boundaries.
- [Production-readiness scaling audit](docs/production-readiness-audit.md):
  evidence-backed risks, target architecture, capacity model, and roadmap for
  monitoring thousands of streams.
- [Distributed implementation progress](docs/distributed-implementation-progress.md):
  active gate checklist and evidence status for implementing that roadmap.
- [Security and identity](docs/security.md): production Compose/VM mTLS worker
  identity, OIDC operator access, input limits, and egress controls.
- [Durable control-plane foundation](docs/durable-control-plane.md):
  PostgreSQL schema/migrations, fenced transaction primitives, JetStream
  outbox, cutover, backup/restore, evidence, and explicit open gates.
- [Compatibility report](docs/compatibility-report.md): tested receivers.
- [Stability report](docs/stability-report.md): soak harness and pending
  long-run evidence.
- [Test plan](TEST_PLAN.md), [acceptance matrix](ACCEPTANCE_MATRIX.md),
  [known limitations](KNOWN_LIMITATIONS.md), and [test gaps](TEST_GAPS.md).
