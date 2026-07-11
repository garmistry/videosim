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
- SQLite feed registration persists feed definitions and alert profiles only.
  Local feed subprocesses are intentionally not restored as running processes
  after a GUI restart.
- External DASH validation supports reachable MPDs with common `SegmentURL` or
  `SegmentTemplate` media references. Unusual DASH packaging may need a new
  resolver in the validation layer.
- Master/worker monitoring remains a transitional distributed slice. On the
  default trusted-lab Compose path, the GUI process is the only master, workers
  poll it over plain HTTP, and worker identity is caller supplied. Versioned
  process-instance/generation/token
  fencing, server-derived report scope, retained-state pruning, and independent
  heartbeat prevent known stale-report and long-batch TTL failures in this
  single-process design, but they are not durable authenticated leases.
- Strict versioned worker reports are the default. The explicit
  `--allow-legacy-worker-reports` compatibility mode cannot fence stale
  generations and is unsuitable for production.
- The production Compose/VM overlay enforces mTLS worker certificates and OIDC
  viewer/admin identities through Nginx plus oauth2-proxy, but a real
  organization OIDC tenant, automated certificate rotation/revocation, and
  durable identity/audit records are not implemented in the repository.
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

- The separate monitor app uses a shared JSON state file rather than a database
  or event broker.
- Feed definitions and alert profiles are persisted by the GUI, but monitor
  alarm/event history still lives in the shared JSON monitor state file.
- Worker registration and assignment fencing are in-memory with a 60-second
  TTL. Independent heartbeats keep long probe batches registered, but worker
  assignments remain simple round-robin and are not capacity-aware.
- Worker report aggregation still writes the shared JSON monitor state file,
  not a database-backed alarm/event history.
- The default direct-app/trusted-lab path has no worker authentication or TLS;
  the production Compose/VM proxy overlay adds mTLS. Neither path yet has
  durable lease fencing or high-availability master failover.
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
