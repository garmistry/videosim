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
  durable incarnation/offer/ack/epoch/config/sequence fencing and atomically
  store scoped probe results plus catalog monitor observations in PostgreSQL.
- Strict versioned worker reports are the default. The explicit
  `--allow-legacy-worker-reports` compatibility mode cannot fence stale
  generations and is unsuitable for production.
- The production Compose/VM overlay enforces mTLS worker certificates and OIDC
  viewer/admin identities through Nginx plus oauth2-proxy, but a real
  organization OIDC tenant and automated certificate rotation/revocation are
  not repository-proven. PostgreSQL selectively persists authentication/
  authorization denials and successful persistent feed/profile mutations, but
  allowed reads/workers/local runtime actions remain stdout-only, denial writes
  are best-effort during storage outages, and no external/WORM audit archive
  exists.
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
  gates. `capacity-check` now rejects incomplete or tampered F5 bundles, but no
  production-like workload result exists for it to admit. `worker-benchmark`
  runs real probes from a supplied state scenario, but measures one worker and
  does not provide fixtures, distributed load, failure injection, or a capacity
  decision. `fixture-fleet` provides deterministic DASH and SRT healthy/slow/
  dead/malformed matrices. `fixture-scenario` can expand them into 1,000 unique
  logical streams, but those streams reuse eight physical endpoints and provide
  neither distributed load nor capacity evidence. `worker-benchmark` can now
  fail unless every assignment starts validation across measured cycles; that
  proves cursor coverage, not detection freshness or sustained capacity.
- The in-process control-plane benchmark can inject worker loss and proves
  survivor coverage plus stale-report rejection. The current round-robin local
  scheduler moved 1,174 assignments for a 1,300-stream/ten-worker/one-loss run
  where only 130 moves were required; stable durable placement remains open.

## Monitoring

- SQLite/trusted-lab monitoring still uses a shared JSON state file. In
  PostgreSQL worker-v2 mode, catalog monitor observations, pending/current
  alarms, and event edges project atomically into PostgreSQL and operator
  `/state.json` reads that database projection instead of the file.
- Outbox delivery is at least once. JetStream's duplicate window cannot replace
  a durable consumer inbox; no consumer/projection is deployed yet. Coordinated
  DB/broker recovery, PITR, and measured RPO/RTO remain unproven.
- PostgreSQL retains bounded per-stream monitor event history (1,000 rows and
  seven days by default) plus current pending/alarm state. It is not yet a
  replayed consumer read model, and SQLite retains file-backed history.
- SQLite worker v1 registration/fencing is in-memory with a 60-second TTL.
  PostgreSQL worker v2 membership/leases are durable and heartbeat-renewed.
  Workers can advertise a static `capacity.maxStreams` limit, but unconfigured
  workers still use compatibility round-robin scheduling and execution remains
  serial unless `--max-concurrent-checks` is configured. The pool is bounded
  stream-level concurrency; with `--batch-budget-seconds`, probe submissions are
  also bounded to one concurrency-sized window. This is not measured capacity
  or a durable fleet queue.
  `--max-srt-streams` and `--max-dash-streams` add static protocol admission
  caps, but they are operator counts rather than measured weighted check costs
  and do not provide tenant fairness. `--max-concurrent-deep-checks` adds a
  separate deep-phase concurrency token, but both phases share one executor and
  the limits remain operator settings rather than measured cost admission.
  `--stream-budget-seconds` defers remaining checks after budget exhaustion but
  now caps built-in media subprocess and DASH polling/socket waits. Arbitrary
  injected checker code and a trickling HTTP response are not preempted.
  Concurrent workers finish each admitted validation window before deep
  standards checks. `--deep-check-interval-seconds` can stagger TR-101,
  frame-rate, and loudness work, but the interval is an operator setting.
  `--batch-budget-seconds` stops new validation windows after budget exhaustion
  and rotates skipped streams, but in-flight probes can still consume their
  per-stream deadlines and there is no durable queue or probe-cost fairness.
  Worker API v2 has an
  authenticated-encrypted, fsynced, byte-bounded report spool that pauses new
  probes behind undelivered reports. Heartbeats persist a bounded latest
  pressure snapshot and durable `/state.json` exposes it. The durable scheduler
  excludes a spool-blocked worker and reports resulting shortfall, but there is
  no queue, pressure history, hysteresis, alerting, recovery SLO evaluation,
  automatic key rotation, quarantine/repair tool, priority eviction, or
  production outage/disk-pressure evidence. Batch CPU and worker/child peak RSS
  are exposed; RSS is a cumulative single-process peak, descriptor count is
  Linux-only, and neither replaces representative resource/capacity runs.
  Black/frozen validation can still
  be expensive and there are no weighted check-cost or tenant tokens.
- PostgreSQL worker-v2 reports now commit direct monitor projection atomically
  with fenced results, so a local JSON write cannot lag operator reads. The
  future JetStream consumer must still use `consumer_inbox` atomically and prove
  replay parity; it is not implemented.
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
