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
- The candidate fixture-domain deployment requires a Linux Docker engine with
  host networking. One local full-shape startup produced exact 220-SRT and
  220-DASH distinct endpoint inventories and sampled one healthy media path per
  protocol. A physical stop/restart made both samples fail and recover. Later
  sequential one-worker runs attempted all 220 distinct SRT URLs and all 220
  distinct DASH URLs with complete behavior-level coverage, but did not drive
  durable assignments or alarms and used one Docker VM. Each host still shares
  one encoder or generator per protocol; this is not independent-host,
  per-stream source independence, failure-domain headroom, or 1,000-stream
  capacity evidence.
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
  gates. `capacity-check` now rejects incomplete or tampered F5 bundles. The
  checked-in three-domain candidate passes its workload contract, but no
  production-like result exists for the verifier to admit. `control-plane-load`
  can raise and clear exact `feed_reachable` alarms for all 1,320 synthetic
  leases and verify their outbox transitions, but it opens no endpoints and
  does not prove real source failure or recovery. `worker-benchmark`
  runs real probes from a supplied state scenario, but measures one worker and
  does not provide distributed load, failure injection, or a capacity decision.
  For fixture scenarios it reports bounded healthy/slow/dead/malformed coverage
  and outcomes. `fixture-fleet` provides deterministic DASH and SRT healthy/slow/
  dead/malformed matrices. Legacy four-endpoint states still make 1,000 logical
  streams reuse eight URLs; the 500-URL manifests instead compose 1,000 unique
  URLs, but their healthy streams share one source per protocol and provide
  neither independent generation nor capacity evidence. `worker-benchmark` can
  fail unless every assignment starts validation at least twice within cycle and
  wall-time limits. The wall-time metric is a conservative cycle-boundary upper
  bound, not a per-probe timestamp or an approved detection-freshness SLO, and
  neither gate proves sustained capacity.
- The 1,320-distinct-URL fixture input represents 32% logical headroom, but its
  ten-worker single-host run failed the 90-second freshness gate on two shards
  and produced 1,598 timeouts. It uses shared protocol sources and does not
  inject or recover from a real failure-domain loss, so it is negative
  saturation evidence rather than F5 capacity evidence.
- `import-fixture-scenario` now gives the composed state a fail-closed,
  transaction-atomic path into an exclusive durable feed catalog and verifies
  exact persisted parity. Its report and local seven-page API check prove
  catalog identity only. They do not prove endpoint reachability, worker probe
  freshness, alarm transitions, source independence, host headroom, or
  admission capacity.
- Scaled SRT listeners retain distinct URLs and ports but share one multicast
  source. Every live endpoint now uses one downstream-leaky relay process:
  batching 16 sinks reduced memory/process pressure, but a concurrent first
  sweep reached only 136/176 healthy paths and the next reached 0/176 while the
  relays stayed alive. Process isolation restored repeated reconnect progress
  at the cost of the higher process/memory shape measured before batching.
- Passive SRT validation uses independent typed video/audio probes and accepts
  captions only when `h264ccextractor` emits bytes. Caption checks try bounded
  receiver shapes with and without an audio drain so normal and video-only
  feeds remain distinguishable inside the 15-second stream budget. This needs
  multiple SRT handshakes. The earlier 171/176 combined-receiver diagnostic is
  superseded because process success did not prove every requested branch.
- The fail-closed durable startup workflow passes both its marked eight-path
  contract and a digest-pinned exact-440/11-worker same-host run. The full shard
  produced balanced 40-stream leases, all 176 healthy paths per protocol, exact
  slow/dead/malformed outcomes, 44 expected malformed-DASH alarms, and zero
  false black/frozen alarms with byte-backed SRT captions. Its loaded SRT source
  used 429.73% CPU, 1.633 GiB, and 1,912 PIDs. This proves one shared-source
  domain, not independent-host headroom, domain-loss recovery, or admission.
  The earlier 148/176 combined-receiver result remains superseded diagnosis.
- Correcting the budget does not make the current single-host environment
  sufficient. A 22-survivor, 12-token, 15-second run failed the 90-second gate
  on 14 workers and saturated local CPU/process capacity. This repository has
  a 33-worker/three-domain candidate sized to retain those 22 survivors, but no
  measured multi-host deployment to replace that failed run.
- The reusable worker-domain Compose file renders 11 uniquely identified,
  capacity-bounded workers per host. A host-local startup workflow validates
  certificates, image/process identity, two health API paths, restart counts,
  exact accepted worker/incarnation registrations, and Docker logs; a local
  durable HTTP run registered all 11 workers. No production immutable image
  over the remote HTTPS/mTLS path, three-host boot, host sizing, media load, or
  physical domain-loss execution has passed.
- The cross-domain startup preflight validates three advertised fixture hosts,
  three worker zones, exact endpoint/worker counts, 33 unique accepted worker
  incarnations, retained state hashes, and one immutable image digest, and
  requires six distinct bounded Linux Docker Engine identities. Engine IDs
  prevent one daemon from masquerading under multiple advertised names, but
  collected JSON cannot prove that distinct daemons map to separate physical
  failure domains. The report therefore keeps `independentHostsCertified=false`
  and `capacityCertified=false`.
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
  worker v2 durable lease fencing. PostgreSQL serializes each assignment
  decision with transaction-scoped leadership, but there is no replicated
  deployment, persistent scheduler leader, or HA storage failover.
- PostgreSQL GUI list reads are bounded to a 100-row React page and a 200-row
  API maximum; overview omits per-stream probe rows and detail reads one feed.
  A replica without matching local runtime presents external configuration as
  writable with runtime unknown, while generated configuration remains
  read-only. Durable forms require a config version, external update/alert/delete
  uses PostgreSQL compare-and-swap from any replica, API startup does not
  preload feed rows, and UUID-backed creates avoid sequential ID collisions.
  Cursors are not cross-request snapshots; generated runtime ownership,
  idempotent create/retry, and production load-balancer/HA behavior remain open.
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
