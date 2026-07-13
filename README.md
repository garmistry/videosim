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
spool-occupancy fields. This is current state, not historical capacity evidence.
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
