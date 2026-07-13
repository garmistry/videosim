# TEST_PLAN.md

# Video Feed Simulator Test Plan

## Test Weights

| Priority | Weight | Meaning |
|---|---:|---|
| P0 | 5 | Critical MVP behavior |
| P1 | 3 | Important reliability or UX behavior |
| P2 | 2 | Useful but non-blocking behavior |
| P3 | 1 | Polish or edge behavior |

Weighted coverage is `covered test weight / total expected test weight`.

## Added DASH Protocol Coverage

DASH is an added protocol option beyond the original SRT MVP. It reuses the six
required simulation modes and is covered by profile, CLI pipeline, GUI protocol,
validator, and live smoke checks:

| Test area | Priority | Status | Command |
|---|---:|---|---|
| DASH profile mapping for all six modes | P1 | implemented | `python3 -m unittest tests.test_profiles` |
| DASH pipeline command generation | P1 | implemented | `python3 -m unittest tests.test_cli_video_feed` |
| GUI protocol selection and DASH endpoint | P1 | implemented | `python3 -m unittest tests.test_gui` |
| DASH manifest/segment validation | P1 | implemented | `python3 -m unittest tests.test_validator` |
| DASH live six-mode smoke | P1 | manual smoke implemented | documented in `docs/work-log.md` |

## Added Multi-Feed GUI Coverage

Multiple feed instances are additive beyond the original single-feed SRT MVP.

| Test area | Priority | Status | Command |
|---|---:|---|---|
| Stream create/list/select payload | P1 | implemented | `python3 -m unittest tests.test_gui` |
| Zero-feed startup state | P1 | implemented | `python3 -m unittest tests.test_gui` |
| Feed detail deep links | P1 | implemented | `python3 -m unittest tests.test_gui` |
| Stream update and delete | P1 | implemented | `python3 -m unittest tests.test_gui` |
| Independent subprocess launch per stream | P1 | implemented | `python3 -m unittest tests.test_gui` |
| Real-time per-stream GUI metrics payload, polling, and 5-minute detail graphs | P1 | implemented | `python3 -m unittest tests.test_gui`; `npm run build-ui` |
| Multi-stream Docker GUI smoke | P1 | manual smoke implemented | documented in `docs/work-log.md` |

## Added External Feed Registration Coverage

Bring-your-own feed registration is additive to generated local feeds. SQLite is
the first persistence adapter behind the feed-store boundary.

| Test area | Priority | Status | Command |
|---|---:|---|---|
| SQLite feed registration store round trip | P1 | implemented | `python3 -m unittest tests.test_feed_store` |
| GUI persists feed definitions and alert profiles through the store boundary | P1 | implemented | `python3 -m unittest tests.test_gui` |
| GUI shows only source-specific create/update fields | P1 | implemented | `python3 -m unittest tests.test_gui`; `npm run build-ui` |
| External SRT URL registration and protocol validation | P1 | implemented | `python3 -m unittest tests.test_gui` |
| Monitor preserves external SRT endpoints and raises selected missing-video alarms | P1 | implemented | `python3 -m unittest tests.test_monitor` |
| External DASH manifest and segment URL validation | P1 | implemented | `python3 -m unittest tests.test_validator` |

## Added Monitoring Coverage

| Test area | Priority | Status | Command |
|---|---:|---|---|
| Monitor alarm raise/repeat/clear cadence | P1 | implemented | `python3 -m unittest tests.test_monitor` |
| Monitor detects missing expected video/audio from validator reports | P1 | implemented | `python3 -m unittest tests.test_monitor` |
| Per-stream alert profile UI, filtering, enable/disable lifecycle, and alarm delay | P1 | implemented | `python3 -m unittest tests.test_monitor tests.test_gui` |
| TR 101 290 priority 1/2 MPEG-TS analyzer indicators | P1 | implemented | `python3 -m unittest tests.test_tr101` |
| TR 101 290 priority 3 PSI/SI and unreferenced-PID parser indicators | P1 | implemented | `python3 -m unittest tests.test_tr101 tests.test_monitor` |
| TR 101 290 priority 3 T-STD timing indicators | P1 | implemented | `python3 -m unittest tests.test_tr101 tests.test_monitor` |
| Monitor converts TR 101 290 analyzer hits into alarms/events | P1 | implemented | `python3 -m unittest tests.test_monitor` |
| Monitor samples DASH TS segments for TR 101 290 alarms | P1 | implemented | `python3 -m unittest tests.test_monitor` |
| Monitor samples malformed DASH fixtures for every TR 101 290 indicator | P1 | implemented | `python3 -m unittest tests.test_monitor` |
| Docker monitor fixture gate for malformed DASH TR 101 290 indicators | P1 | implemented | `docker compose run --build --rm monitor-fixtures` |
| Feed frame-rate selection and fractional GStreamer caps | P1 | implemented | `python3 -m unittest tests.test_framerate tests.test_cli_video_feed tests.test_profiles tests.test_gui` |
| Monitor raises configured-vs-measured frame-rate mismatch alarm | P1 | implemented | `python3 -m unittest tests.test_monitor tests.test_framerate` |
| Audio loudness parser and DASH audio segment selection | P1 | implemented | `python3 -m unittest tests.test_loudness` |
| Monitor raises ITU-R BS.1770, EBU R 128, and ATSC A/85 loudness alarms | P1 | implemented | `python3 -m unittest tests.test_monitor tests.test_loudness` |
| GUI payload and fallback render monitor alarms | P1 | implemented | `python3 -m unittest tests.test_gui` |
| React renders alarm and event audit panels | P1 | implemented | `python3 -m unittest tests.test_gui`; `npm run build-ui` |

## Added Production Security Boundary Coverage

| Test area | Priority | Status | Command |
|---|---:|---|---|
| Trusted proxy secret is required and compared before identity headers are accepted | P0 | implemented | `python3 -m unittest tests.test_security` |
| mTLS-derived worker identity must exactly match requested/reported worker ID | P0 | implemented | `python3 -m unittest tests.test_security` |
| OIDC viewer/admin groups enforce read versus mutation authorization | P0 | implemented | `python3 -m unittest tests.test_security` |
| Health endpoints remain public while control-plane endpoints require identity | P1 | implemented | `python3 -m unittest tests.test_security` |
| Trusted mode rejects inline credentials and unsafe destination addresses by default | P0 | implemented | `python3 -m unittest tests.test_security` |
| Request bodies, report collections, and identifiers have hard limits | P0 | implemented | `python3 -m unittest tests.test_security tests.test_worker_api tests.test_gui` |
| Worker TLS context loads CA and client certificate/key | P0 | implemented | `python3 -m unittest tests.test_worker` |
| Transient transport failures use bounded jittered retries while 409 refetch remains distinct | P0 | implemented | `python3 -m unittest tests.test_worker` |
| Production Compose interpolation and Nginx TLS/mTLS configuration validate | P1 | implemented | `docker compose -f docker-compose.production.yml config --quiet`; Nginx `-t` smoke in `docs/work-log.md` |
| Live worker certificate CN accepted and mismatched worker ID denied through Nginx | P0 | implemented smoke | documented in `docs/work-log.md` |

## Added Distributed Monitoring Coverage

The master/worker control-plane slice is additive to the local monitor service.

| Test area | Priority | Status | Command |
|---|---:|---|---|
| Master assigns running streams across active workers | P1 | implemented | `python3 -m unittest tests.test_gui` |
| Master rewrites generated DASH monitor endpoints for workers | P1 | implemented | `python3 -m unittest tests.test_gui tests.test_monitor` |
| Master merges worker alarm/event reports without clearing other workers' streams | P1 | implemented | `python3 -m unittest tests.test_gui` |
| Worker CLI loop polls assignments, reuses monitor checks, and posts reports | P1 | implemented | `python3 -m unittest tests.test_worker tests.test_cli_video_feed` |
| Versioned instance/generation/token fencing rejects stale, restart, ABA, and token-mismatch reports | P0 | implemented | `python3 -m unittest tests.test_gui tests.test_worker_api` |
| Server-derived report scope rejects forged IDs and scopes alarms, events, pending state, and probe metrics | P0 | implemented | `python3 -m unittest tests.test_gui tests.test_worker` |
| Independent heartbeat keeps a worker registered during a probe batch beyond the configured TTL | P0 | implemented | `python3 -m unittest tests.test_worker` |
| Worker prunes retained state and refetches after bounded HTTP 409 conflicts | P0 | implemented | `python3 -m unittest tests.test_worker` |
| Worker API returns typed 409/422/503 failures and never acknowledges failed persistence | P0 | implemented | `python3 -m unittest tests.test_worker_api tests.test_gui` |
| Probe metrics classify bounded latest-batch success/issue/error/timeout/skipped outcomes | P1 | implemented | `python3 -m unittest tests.test_monitor tests.test_gui` |
| Concurrent stream allocation and assignment snapshots preserve unique IDs, ports, and ownership | P0 | implemented | `python3 -m unittest tests.test_gui` |
| In-process control-plane benchmark enforces assignment/report invariants and disclaims media capacity | P1 | implemented | `python3 -m unittest tests.test_distributed_benchmark`; `python3 -m videosim control-plane-benchmark --streams 1000 --workers 10 --iterations 3` |
| Compose exposes a sample worker node service | P1 | implemented | `docker compose config --quiet` |
| Worker SIGINT/SIGTERM sends its final report before an incarnation-fenced drain and immediate reoffer | P0 | implemented; PostgreSQL integration gated | `python3 -m unittest tests.test_worker tests.test_worker_api`; `VIDEOSIM_TEST_POSTGRES_URL=... python3 -m unittest tests.test_postgres_store tests.test_worker_v2` |
| Critical validation runs every cycle while deep checks defer on stable per-stream cadence offsets without false alarm clears | P0 | implemented | `python3 -m unittest tests.test_monitor tests.test_worker tests.test_cli_video_feed tests.test_gui` |
| Aggregate batch pressure defers only the deep phase after every assigned stream validates, preserving alarms and staggered retry state | P0 | implemented | `python3 -m unittest tests.test_monitor tests.test_worker tests.test_cli_video_feed tests.test_gui` |
| Worker v2 reports are encrypted and fsynced before send, replay before registration, remain byte-bounded, and block new probes during transport outage | P0 | implemented unit; production outage/chaos pending | `python3 -m unittest tests.test_report_spool tests.test_worker tests.test_cli_video_feed` |

## Startup Workflow Coverage

| Test area | Priority | Status | Command |
|---|---:|---|---|
| Compose app becomes ready and worker registers through the HTTP API | P0 | implemented integration | `VIDEOSIM_STARTUP_INTEGRATION=1 python3 -m unittest tests.test_startup_workflow` |
| API creates, starts, validates, and stops a normal SRT feed with video/audio/captions present | P0 | implemented integration | same startup command |
| Docker process state and service logs are captured; Python traceback fails the gate | P1 | implemented integration | same startup command; `artifacts/startup-validation/` |
| Google Chrome operator repeats start/validate/stop and checks endpoint/logs | P1 | documented manual check | `RUNBOOK.md` startup validation workflow |

## Durable Control-Plane Foundation Coverage

These tests require disposable PostgreSQL/NATS services and do not run merely
because the unit suite is green.

| Test area | Priority | Status | Command |
|---|---:|---|---|
| Migration application is idempotent, serialized, checksum/name guarded, and transactionally reversible | P0 | implemented integration | `VIDEOSIM_TEST_POSTGRES_URL=... python3 -m unittest tests.test_postgres_store` |
| Feed config versions and retry-safe SQLite cutover import | P0 | implemented integration | same PostgreSQL command |
| Fresh worker offers/acknowledges incarnation/config/epoch lease; stale heartbeat cannot renew/ingest; new epoch restarts sequence at 1 while same epoch preserves it | P0 | implemented integration | same PostgreSQL command |
| Fenced ingestion rejects stale incarnation/epoch/config/sequence, reordered/future observations, and immutable-ID payload drift | P0 | implemented integration | same PostgreSQL command |
| Accepted result/current state/alarm edges/outbox commit atomically; duplicates are idempotent and inconclusive evidence never clears | P0 | implemented integration | same PostgreSQL command |
| Audit event/outbox identity/content are database-enforced immutable while publisher delivery fields remain writable | P1 | implemented integration | same PostgreSQL command |
| Concurrent outbox claims are disjoint/publisher-fenced; failed rows retry with bounded backoff/dead state; event-ID drift fails | P0 | implemented integration | same PostgreSQL command |
| JetStream stream uses file storage/required subjects and stores publish acknowledgement sequence | P0 | implemented integration | `VIDEOSIM_TEST_POSTGRES_URL=... VIDEOSIM_TEST_NATS_URL=... python3 -m unittest tests.test_nats_publisher` |
| Backup restore rejects non-owner/reused-runtime-password targets before destructive work, restores schema/data, increments informational generation, expires restored leases, applies explicit broker mode, and matches semantic hash | P0 | local functional evidence only | `python3 -m unittest tests.test_restore_scripts`; commands/evidence in `docs/work-log.md`; production PITR/RPO/RTO open |
| PostgreSQL HTTP worker v2 performs incarnation registration, offer/ack, non-resurrecting heartbeat renewal, immutable report ingestion, per-lease epoch sequence restart, config fence, direct DB projection duplicate/reassignment fencing, and two-worker handoff | P0 | implemented integration | `VIDEOSIM_TEST_POSTGRES_URL=... python3 -m unittest tests.test_worker_v2`; `python3 -m unittest tests.test_worker` |
| PostgreSQL rejects worker v1 downgrade; SQLite keeps v1 lab compatibility | P0 | implemented integration | same worker commands |
| Catalog monitor observations project pending/delay/repeat/clear/suppress state atomically; omitted/inconclusive evidence preserves state; `probe.*` never becomes a UI alarm; PostgreSQL `/state.json` reads one snapshot; write-time and scheduled global retention bound active and stopped-stream event history | P0 | implemented integration | `VIDEOSIM_TEST_POSTGRES_URL=... python3 -m unittest tests.test_postgres_store tests.test_worker_v2`; `python3 -m unittest tests.test_monitor tests.test_control_plane tests.test_cli_video_feed` |
| Selective durable HTTP audit: bounded proxy/worker/operator denials, atomic successful persistent feed/profile mutations, query-path sanitization, fail-closed mutation errors, and cross-process delete/recreate-ABA lifecycle fencing | P0 | implemented integration | `VIDEOSIM_TEST_POSTGRES_URL=... python3 -m unittest tests.test_worker_v2 tests.test_postgres_store`; `python3 -m unittest tests.test_gui` |
| Runtime-role convergence removes unsafe membership/grants/ownership; app/publisher/pruner paths retain only their required permissions | P1 | implemented integration | set `VIDEOSIM_TEST_POSTGRES_APP_URL`, `VIDEOSIM_TEST_POSTGRES_PUBLISHER_URL`, and `VIDEOSIM_TEST_POSTGRES_PRUNER_URL`, then run `tests.test_postgres_store` |
| Inbox-deduplicated JetStream consumer/replay projection | P0 | not implemented | remaining F2 cutover gate |

## Milestone Gates

Each milestone may advance only when all P0 tests for that milestone are
implemented and passing, weighted coverage meets the milestone threshold, blocker
and critical defects are closed, gaps are recorded in [TEST_GAPS.md](TEST_GAPS.md),
and a human-visible demo path exists.

Core outage milestones require stricter coverage:

- Static outage profiles: 95%.
- GUI outage selection: 95%.
- Runtime fault toggles: 95%.
- Version 1.0: 98%.

## Critical Requirement Test Mapping

| Requirement ID | Critical requirement | Planned acceptance tests |
|---|---|---|
| CR1 | Run on Linux. | M1-P0-start-srt-video-feed, M7-P0-gui-launches, M11-P0-gstreamer-receiver-consumes-required-modes |
| CR2 | Provide a visual GUI. | M7-P0-gui-launches, M7-P0-gui-starts-normal-feed, M8-P0-gui-starts-all-required-modes |
| CR3 | Generate local SRT feeds. | M1-P0-start-srt-video-feed, M1-P0-receiver-detects-video, M5-P0-all-static-profiles-validate |
| CR4 | Generate a normal feed with video, audio, and closed captions. | M2-P0-normal-feed-contains-video, M2-P0-normal-feed-contains-audio, M3-P0-captions-present-in-normal-feed |
| CR5 | Support all required fault modes. | M5-P0-normal-profile-validates, M5-P0-audio-only-profile-validates, M5-P0-video-only-profile-validates, M5-P0-no-caption-profile-validates, M5-P0-black-video-profile-validates, M5-P0-frozen-video-profile-validates |
| CR6 | Show a copyable SRT endpoint URL. | M7-P0-gui-shows-copyable-srt-url |
| CR7 | Provide start/stop controls. | M1-P0-stop-feed, M1-P0-restart-feed, M7-P0-gui-stops-feed |
| CR8 | Provide basic logs. | M1-P1-log-output-includes-start-stop-failure, M10-P0-logs-visible |
| CR9 | Provide clear error states. | M1-P1-invalid-port-handling, M10-P0-failed-feed-shows-error |
| CR10 | Provide validation that proves actual stream state. | M6-P0-validate-normal-feed, M6-P0-validate-audio-only-feed, M6-P0-validate-video-only-feed, M6-P0-validate-no-caption-feed, M6-P0-validate-black-video-feed, M6-P0-validate-frozen-video-feed, M6-P0-validate-stopped-feed |
| CR11 | Include install/run documentation. | M0-P0-required-docs-exist, M0-P0-runbook-has-install-and-verify-path |
| CR12 | Include tests for all critical behavior. | M0-P0-critical-requirements-map-to-tests, M0-P0-required-feed-modes-map-to-tests |

## Required Feed Mode Matrix

| Mode | Video track | Video content | Audio track | Captions | Planned acceptance tests |
|---|---|---|---|---|---|
| normal | present | moving/generated | present | present | M5-P0-normal-profile-validates, M6-P0-validate-normal-feed, M8-P0-gui-starts-normal-mode |
| audio_only | absent | n/a | present | absent or explicitly unsupported | M5-P0-audio-only-profile-validates, M6-P0-validate-audio-only-feed, M8-P0-gui-starts-audio-only-mode |
| video_only | present | moving/generated | absent | present | M5-P0-video-only-profile-validates, M6-P0-validate-video-only-feed, M8-P0-gui-starts-video-only-mode |
| no_captions | present | moving/generated | present | absent | M5-P0-no-caption-profile-validates, M6-P0-validate-no-caption-feed, M8-P0-gui-starts-no-caption-mode |
| black_video | present | black frames | present | present | M5-P0-black-video-profile-validates, M6-P0-validate-black-video-feed, M8-P0-gui-starts-black-video-mode |
| frozen_video | present | static/repeated frame | present | present | M5-P0-frozen-video-profile-validates, M6-P0-validate-frozen-video-feed, M8-P0-gui-starts-frozen-video-mode |

## Milestone 0 Tests

| Test ID | Priority | Weight | Status | Command |
|---|---:|---:|---|---|
| M0-P0-critical-requirements-map-to-tests | P0 | 5 | implemented | `python3 -m unittest tests.test_docs_contract` |
| M0-P0-required-feed-modes-map-to-tests | P0 | 5 | implemented | `python3 -m unittest tests.test_docs_contract` |
| M0-P0-required-docs-exist | P0 | 5 | implemented | `python3 -m unittest tests.test_docs_contract` |
| M0-P0-runbook-has-install-and-verify-path | P0 | 5 | implemented | `python3 -m unittest tests.test_docs_contract` |

Milestone 0 weighted coverage: 20 / 20 = 100%.

## Milestone 1 Tests

| Test ID | Priority | Weight | Status | Command |
|---|---:|---:|---|---|
| M1-P0-start-srt-video-feed | P0 | 5 | implemented | `python3 -m unittest tests.test_cli_video_feed`; `docker compose run --build --rm live-srt` |
| M1-P0-receiver-detects-video | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M1-P0-stop-feed | P0 | 5 | implemented | `python3 -m unittest tests.test_cli_video_feed`; `docker compose run --build --rm live-srt` |
| M1-P0-restart-feed | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M1-P1-invalid-port-handling | P1 | 3 | implemented | `python3 -m unittest tests.test_cli_video_feed` |
| M1-P1-log-output-includes-start-stop-failure | P1 | 3 | implemented | `python3 -m unittest tests.test_cli_video_feed` |

Milestone 1 weighted coverage: 26 / 26 = 100%.

## Milestone 2 Tests

| Test ID | Priority | Weight | Status | Command |
|---|---:|---:|---|---|
| M2-P0-normal-feed-contains-video | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M2-P0-normal-feed-contains-audio | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M2-P0-receiver-consumes-audio-video-feed | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M2-P1-audio-continuity-30-minutes | P1 | 3 | not implemented | tracked in `TEST_GAPS.md` |
| M2-P1-audio-config-error-handling | P1 | 3 | implemented | `python3 -m unittest tests.test_cli_video_feed` |

Milestone 2 weighted coverage: 18 / 21 = 85.7%.

## Milestone 3 Tests

| Test ID | Priority | Weight | Status | Command |
|---|---:|---:|---|---|
| M3-P0-captions-present-in-normal-feed | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M3-P0-captions-can-be-detected | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M3-P0-video-audio-still-present-when-captions-enabled | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M3-P1-caption-text-updates-over-time | P1 | 3 | not implemented | tracked in `TEST_GAPS.md` |
| M3-P1-caption-disabled-negative-test | P1 | 3 | implemented | `docker compose run --build --rm live-srt` |

Milestone 3 weighted coverage: 18 / 21 = 85.7%.

## Milestone 4 Tests

| Test ID | Priority | Weight | Status | Command |
|---|---:|---:|---|---|
| M4-P0-valid-normal-profile-runs | P0 | 5 | implemented | `python3 -m unittest tests.test_profiles` |
| M4-P0-invalid-profile-fails-safely | P0 | 5 | implemented | `python3 -m unittest tests.test_profiles` |
| M4-P0-profile-maps-to-expected-video-audio-caption-state | P0 | 5 | implemented | `python3 -m unittest tests.test_profiles` |
| M4-P1-missing-fields-reported-clearly | P1 | 3 | implemented | `python3 -m unittest tests.test_profiles` |
| M4-P1-schema-version-checked | P1 | 3 | implemented | `python3 -m unittest tests.test_profiles` |

Milestone 4 weighted coverage: 21 / 21 = 100%.

## Milestone 5 Tests

| Test ID | Priority | Weight | Status | Command |
|---|---:|---:|---|---|
| M5-P0-normal-profile-validates | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M5-P0-audio-only-profile-validates | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M5-P0-video-only-profile-validates | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M5-P0-no-caption-profile-validates | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M5-P0-black-video-profile-validates | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M5-P0-frozen-video-profile-validates | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M5-P1-unknown-outage-mode-fails-clearly | P1 | 3 | implemented | `python3 -m unittest tests.test_profiles` |
| M5-P1-receiver-does-not-crash-on-any-outage-scenario | P1 | 3 | implemented | `docker compose run --build --rm live-srt` |

Milestone 5 weighted coverage: 36 / 36 = 100%.

## Milestone 6 Tests

| Test ID | Priority | Weight | Status | Command |
|---|---:|---:|---|---|
| M6-P0-validate-normal-feed | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M6-P0-validate-audio-only-feed | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M6-P0-validate-video-only-feed | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M6-P0-validate-no-caption-feed | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M6-P0-validate-black-video-feed | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M6-P0-validate-frozen-video-feed | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M6-P0-validate-stopped-feed | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M6-P1-false-positive-check | P1 | 3 | implemented | absent-track, no-caption, and stopped-feed live checks |
| M6-P1-false-negative-check | P1 | 3 | implemented | all expected-present live profile checks |
| M6-P1-clear-validation-failure-messages | P1 | 3 | implemented | `python3 -m unittest tests.test_validator` |

Milestone 6 weighted coverage: 44 / 44 = 100%.

## Milestone 7 Tests

| Test ID | Priority | Weight | Status | Command |
|---|---:|---:|---|---|
| M7-P0-gui-launches | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M7-P0-gui-starts-normal-feed | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M7-P0-receiver-consumes-gui-started-feed | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M7-P0-gui-stops-feed | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M7-P1-logs-shown-in-gui | P1 | 3 | implemented | `python3 -m unittest tests.test_gui` |
| M7-P1-backend-start-failure-shown-clearly | P1 | 3 | implemented | `python3 -m unittest tests.test_gui` |

Milestone 7 weighted coverage: 26 / 26 = 100%.

## Milestone 8 Tests

| Test ID | Priority | Weight | Status | Command |
|---|---:|---:|---|---|
| M8-P0-gui-starts-normal-mode | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M8-P0-gui-starts-audio-only-mode | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M8-P0-gui-starts-video-only-mode | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M8-P0-gui-starts-no-caption-mode | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M8-P0-gui-starts-black-video-mode | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M8-P0-gui-starts-frozen-video-mode | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M8-P0-gui-state-matches-validation-output | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M8-P1-mode-labels-understandable | P1 | 3 | implemented | `python3 -m unittest tests.test_gui` |
| M8-P1-invalid-combinations-blocked | P1 | 3 | implemented | `python3 -m unittest tests.test_gui` |

Milestone 8 weighted coverage: 41 / 41 = 100%.

## Milestone 9 Tests

| Test ID | Priority | Weight | Status | Command |
|---|---:|---:|---|---|
| M9-P0-toggle-video-off-on | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M9-P0-toggle-audio-off-on | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M9-P0-toggle-captions-off-on | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M9-P0-toggle-black-video-off-on | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M9-P0-toggle-frozen-video-off-on | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M9-P0-gui-state-equals-validation-result | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M9-P1-toggle-event-logging | P1 | 3 | implemented | `python3 -m unittest tests.test_gui` |
| M9-P1-contradictory-states-prevented | P1 | 3 | implemented | `python3 -m unittest tests.test_gui` |

Milestone 9 weighted coverage: 36 / 36 = 100%.

## Milestone 10 Tests

| Test ID | Priority | Weight | Status | Command |
|---|---:|---:|---|---|
| M10-P0-failed-feed-shows-error | P0 | 5 | implemented | `python3 -m unittest tests.test_gui` |
| M10-P0-intentional-outage-shown-as-intentional | P0 | 5 | implemented | `python3 -m unittest tests.test_gui` |
| M10-P0-logs-visible | P0 | 5 | implemented | `python3 -m unittest tests.test_gui` |
| M10-P0-logs-exportable | P0 | 5 | implemented | `python3 -m unittest tests.test_gui` |
| M10-P1-validation-output-visible-in-gui | P1 | 3 | implemented | `python3 -m unittest tests.test_gui` |
| M10-P1-error-messages-actionable | P1 | 3 | implemented | `python3 -m unittest tests.test_gui` |

Milestone 10 weighted coverage: 26 / 26 = 100%.

## Milestone 11 Tests

| Test ID | Priority | Weight | Status | Command |
|---|---:|---:|---|---|
| M11-P0-ffplay-consumes-required-modes | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M11-P0-ffprobe-detects-required-tracks | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M11-P0-gstreamer-receiver-consumes-required-modes | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M11-P0-required-target-receivers | P0 | 5 | not applicable | no project-specific target receiver is defined |
| M11-P1-known-limitations-documented | P1 | 3 | implemented | `docs/compatibility-report.md`; `KNOWN_LIMITATIONS.md` |
| M11-P1-vlc-smoke-test | P1 | 3 | not applicable | VLC is not feasible in the minimal headless Docker harness |

Milestone 11 weighted coverage: 18 / 18 = 100% for applicable tests.

## Milestone 12 Tests

| Test ID | Priority | Weight | Status | Command |
|---|---:|---:|---|---|
| M12-P0-normal-feed-24-hour-soak | P0 | 5 | not implemented | tracked in `TEST_GAPS.md` |
| M12-P0-required-outage-soak-tests | P0 | 5 | short proof implemented, long run pending | `docker compose run --build --rm live-srt`; tracked in `TEST_GAPS.md` |
| M12-P0-repeated-start-stop-test | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M12-P0-gui-remains-responsive-during-long-run | P0 | 5 | short proof and runner implemented, long run pending | `docker compose run --build --rm live-srt`; `docker compose run --build --rm m12-gui-soak`; tracked in `TEST_GAPS.md` |
| M12-P1-resource-usage-report | P1 | 3 | implemented | `python3 -m unittest tests.test_soak`; `python3 -m videosim soak-check --report-dir ...`; `docs/stability-report.md` |
| M12-P1-validation-every-15-minutes-during-soak | P1 | 3 | supported by harness and runner, long run pending | `python3 -m unittest tests.test_soak`; `scripts/run-m12-soak.sh` |

Milestone 12 weighted coverage: 11 / 26 = 42.3%; milestone remains open until long-run P0 evidence exists.
