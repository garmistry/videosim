# KNOWN_LIMITATIONS.md

# Known Limitations

- The CLI can start, stop, and restart audio/video/caption SRT listener feeds
  and DASH MPD/segment feeds.
- Runtime fault controls use a controlled stream restart; seamless in-place
  toggles are intentionally out of MVP scope.
- Profile parsing supports only the flat YAML shape used by the current normal
  and outage profiles.
- The Milestone 2 30-minute audio continuity P1 test is not implemented.
- Captions start after a fixed 3-second delay so live SRT video caps negotiate
  before caption bytes reach `cccombiner`.
- The Milestone 3 caption update-over-time P1 test is not implemented.
- The Dockerfile is a Linux test harness, not release packaging.
- Docker packaging is preferred for portability but is not part of the critical
  MVP gate in AGENTS.md; revisit after core feed generation and validation work.
- macOS is treated as a development target. Linux remains the required runtime
  target.
- Audio-only caption behavior is explicitly allowed to be absent/unsupported and
  must be reported clearly by validation.
- SRT captions are embedded CEA-608 in H.264. DASH captions are currently
  emitted as a WebVTT subtitle adaptation in the MPD, not embedded CEA-608.
- Docker Compose publishes UDP 9000-9010 for GUI-managed SRT streams by default.
  Additional SRT streams need more host UDP ports published.
- ffprobe and ffplay compatibility tests are receiver smoke checks; semantic
  caption, black-video, and frozen-video proof remains in `videosim validate`.
- VLC is not tested in the minimal headless Docker harness.
- No project-specific target receiver has been defined.
- The soak harness is implemented, but 24-hour normal/outage soak evidence is
  still pending.
- The GUI preview is a local refreshed frame matching the active mode, not
  native browser SRT playback and not validation proof of the SRT output.
  Audio-only mode and external feeds have no video preview.
- GUI feed metrics are estimates from configured media tracks and elapsed run
  time for generated feeds. They are not actual SRT socket byte counters,
  external-feed ingress counters, or per-receiver telemetry.
- SQLite remains the trusted-lab feed-registration default and persists feed
  definitions/alert profiles only. The production overlay selects PostgreSQL,
  but local feed subprocesses are intentionally not restored after restart.
- External DASH validation supports reachable MPDs with common `SegmentURL` or
  `SegmentTemplate` media references. Unusual DASH packaging may need a new
  resolver in the validation layer.
- Master/worker monitoring remains a transitional distributed slice. On the
  default trusted-lab Compose path, the GUI process is the only master, workers
  poll it over plain HTTP, and worker identity is caller supplied. Versioned
  process-instance/generation/token
  fencing, server-derived report scope, retained-state pruning, and independent
  heartbeat prevent known stale-report and long-batch TTL failures in this
  single-process HTTP path. PostgreSQL-backed deployments now use worker v2
  durable incarnation/offer/ack/epoch/config/sequence fencing and store scoped
  probe results before updating the transitional JSON projection.
- Strict versioned worker reports are the default. The explicit
  `--allow-legacy-worker-reports` compatibility mode cannot fence stale
  generations and is unsuitable for production.
- The production Compose/VM overlay enforces mTLS worker certificates and OIDC
  viewer/admin identities through Nginx plus oauth2-proxy, but a real
  organization OIDC tenant and automated certificate rotation/revocation are
  not repository-proven. A durable audit primitive exists, but HTTP security
  events still emit only to structured stdout until the F2 cutover is complete.
- Trusted-mode destination checks deny private addresses by default and support
  explicit private/suffix policy. They do not replace VM firewall/egress rules
  and do not yet provide redirect-aware DNS rebinding protection.
- The production Compose overlay does not expose generated SRT UDP listener
  ports; generated-feed data-plane placement remains a later architecture gate.
- The in-process control-plane benchmark opens no media and has not certified
  1,000, 5,000, or 10,000 monitored streams; modeled or control-plane-only
  demand must not be treated as measured media capacity. See
  `docs/production-readiness-audit.md` and
  `docs/distributed-implementation-progress.md` for remaining blockers and
  gates.

## Monitoring

- The separate local monitor still uses a shared JSON state file. Worker v2
  durably stores `probe.*` results and then updates that JSON projection, but
  actual monitor-alarm snapshots and operator reads have not moved to the
  PostgreSQL projection yet.
- Outbox delivery is at least once. JetStream's duplicate window cannot replace
  a durable consumer inbox; no consumer/projection is deployed yet. Coordinated
  DB/broker recovery, PITR, and measured RPO/RTO remain unproven.
- Feed definitions and alert profiles are persisted by the GUI, but monitor
  alarm/event history still lives in the shared JSON monitor state file.
- SQLite worker v1 registration/fencing is in-memory with a 60-second TTL.
  PostgreSQL worker v2 membership/leases are durable and heartbeat-renewed, but
  scheduling remains deterministic round-robin rather than capacity-aware.
- Worker v2 report aggregation writes durable probe results and then the shared
  JSON monitor state through a durable no-regression fence. The JSON write is
  retryable but not independently replayed: if a worker dies after DB commit
  and before retrying its shadow write, operator JSON can lag until a later
  report. It is not a durable alarm/event read model.
- The default direct-app/trusted-lab path has no worker authentication or TLS
  and retains worker v1. The production Compose/VM proxy path adds mTLS and
  worker v2 durable lease fencing, but there is no HA scheduler/storage failover.
- TR 101 290 PCR accuracy is estimated from the sampled packet rate. It is good
  for simulator regression alarms, not a replacement for calibrated lab
  measurement equipment.
- TR 101 290 priority 3 T-STD buffer, empty-buffer, and data-delay checks are
  parser-backed timing approximations from sample byte rate and PES PTS, not a
  calibrated ISO decoder buffer model.
- Frame-rate alarms use FFprobe-reported stream rates from the live endpoint or
  latest DASH video segment. They detect configured-rate mismatches, not
  long-term cadence jitter.
- Audio loudness alarms use short live samples through FFmpeg `ebur128`.
  They are useful for operational alarms but are not full-program EBU R 128 or
  ATSC A/85 compliance certificates.
