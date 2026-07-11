# TEST_GAPS.md

# Test Gaps

## Current Gate

Milestones 0 through 11 have no known P0 test gaps. Milestone 1 through 11 live SRT
receiver proof runs in Docker with `docker compose run --build --rm live-srt`.

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
- Automated CA issuance, certificate rotation/revocation, durable identity and
  audit records, multi-tenant resource grants, and firewall policy deployment
  remain future durable/HA gates.
- Application DNS/address checks are defense in depth. DNS rebinding and HTTP
  redirects require VM egress enforcement and future redirect-aware fetch
  policy tests before external production.

## Distributed Architecture

- Foundation coverage now includes versioned process-instance/generation/token
  fencing, server-derived report ownership, retained-state pruning, independent
  heartbeat during a probe batch, typed worker API conflicts/validation/storage
  errors, scoped probe metrics, concurrent assignment/allocation behavior, and
  an in-process assignment/report benchmark.
- The process-local generation/token is not a durable lease and has no
  multi-replica or control-plane failover proof. PostgreSQL/broker-backed
  persistence, authenticated worker identity, idempotent results, and durable
  fencing belong to the next gates in
  `docs/distributed-implementation-progress.md`.
- The control-plane benchmark runs no SRT/DASH probes. Representative media
  load, failure storms, 24-hour soak, security, restore, and 1,000/5,000/10,000
  admission evidence remain missing and must not be inferred from it.
- The independent heartbeat currently lacks retry/backoff metrics and a durable
  worker incarnation. Worker execution remains serial and is not yet
  capacity-aware or backpressured.

## Monitoring

- TR 101 290 priority 1/2 indicators and parser-backed priority 3 PSI/SI plus
  unreferenced-PID and T-STD timing indicators have unit coverage, and malformed
  DASH fixture sampling is covered through the monitor alarm path for every
  indicator. A Docker `monitor-fixtures` gate is defined for that fixture suite.
  Malformed live SRT/DASH fixture streams are not yet generated for end-to-end
  Docker proof of every indicator.
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
