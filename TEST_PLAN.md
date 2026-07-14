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
| Transient transport failures, including a real loopback TCP response reset, use bounded jittered retries and publish bounded retry pressure while 409 refetch remains distinct | P0 | implemented | `python3 -m unittest tests.test_worker` |
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
| In-process control-plane benchmark enforces assignment/report invariants, injects worker loss, rejects stale reports, reports minimum/excess reassignment churn, and disclaims media capacity | P1 | implemented process-local; durable failure pending | `python3 -m unittest tests.test_distributed_benchmark`; `python3 -m videosim control-plane-benchmark --streams 1300 --workers 10 --fail-workers 1 --iterations 3` |
| Worker benchmark runs real monitor/media probes from a hashed exported-state scenario, reports bounded per-protocol and fixture-behavior validation outcomes/coverage, and can fail closed on full coverage plus two starts per stream within bounded initial/repeat/trailing cycle and conservative wall-time gaps | P1 | implemented harness/live cadence and protocol-calibration runs; exact local 220-SRT and 220-DASH distinct-path behavior coverage passed sequentially, while independent-source capacity remains missing | `python3 -m unittest tests.test_worker_benchmark`; `python3 -m videosim worker-benchmark --scenario <state.json> --require-full-validation-coverage --max-validation-gap-cycles <cycles> --max-validation-gap-seconds <seconds> --json` |
| Fixture fleet serves healthy/slow/dead/malformed DASH/SRT URLs, validates optional per-behavior endpoint counts, writes deterministic benchmark states, and isolates every live SRT listener in one relay process fed by the shared encoder | P1 | 500+500 command shape and live smoke passed; exact 220+220 startup and complete sequential SRT/DASH path sweeps passed locally; multi-sink relays failed concurrent reconnects and are no longer used | `python3 -m unittest tests.test_fixture_fleet`; `python3 -m videosim fixture-fleet --manifest scale/fixtures/<protocol>-endpoints-220-domain.json --state-path <state.json>` |
| Fixture scenario composes exact seeded protocol/behavior mixes from one or more state shards, rejects duplicate cross-shard endpoints, and records ordered state hashes/counts | P1 | three simulated source domains compose exact 1,320 distinct URLs; multi-host generation/admission remain | `python3 -m unittest tests.test_fixture_scenario`; `python3 -m videosim fixture-scenario --manifest scale/fixtures/mixed-1320.json --srt-state <a> --srt-state <b> --srt-state <c> --dash-state <a> --dash-state <b> --dash-state <c> --state-path <state.json>` |
| Fixture catalog import validates a composed state, maps complete external-feed configs, rejects shared endpoints and unlisted rows by default, atomically upserts the exclusive durable catalog, verifies persisted parity, and emits a non-capacity report consumable by real worker assignment | P0 | atomic rollback/idempotence and scheduler lease output pass on PostgreSQL; exact local 1,320 import plus seven-page API parity passed, while independent-host media/alarm/admission evidence remains open | `python3 -m unittest tests.test_fixture_scenario`; `VIDEOSIM_TEST_POSTGRES_URL=... python3 -m unittest tests.test_fixture_catalog`; `python3 -m videosim import-fixture-scenario --state <state.json> --output <report.json>` |
| Candidate fixture-domain Compose boots SRT/DASH load services on Linux host networking; startup checks bounded Docker Engine identity plus exact inventory/API/sampled media/resources/process/logs, while the fault workflow stops both origins, proves sampled media failure, restarts, and retains timed recovery evidence with fail-safe restart | P1 | render and marked startup/fault/recovery passed; one exact 220+220 local shard recovered sampled SRT/DASH and later sequential sweeps attempted all 440 paths, while three independent hosts, all-path durable worker alarms/load, and admission remain pending | `python3 -m unittest tests.test_fixture_domain_compose`; `VIDEOSIM_FIXTURE_STARTUP_INTEGRATION=1 python3 -m unittest tests.test_fixture_domain_compose` |
| Durable fixture startup gate boots fixture services, fresh PostgreSQL, migrated/imported catalog, API, and real workers; requires exact lease balance, latest per-behavior media outcomes, alarm shape, paginated API parity, stable containers, resources, and clean Docker logs | P0 | marked eight-path/two-worker Docker workflow passes; a digest-pinned exact-440/11-worker same-host run passed every behavior outcome with byte-backed SRT captions and final loaded fixture resources, while independent-host loss/headroom and 24-hour admission remain open | `python3 -m unittest tests.test_durable_fixture_startup`; `VIDEOSIM_DURABLE_FIXTURE_STARTUP_INTEGRATION=1 VIDEOSIM_DURABLE_FIXTURE_IMAGE=<image> python3 -m unittest tests.test_durable_fixture_startup` |
| F5 cross-domain startup preflight requires three clean digest-pinned fixture result/state triplets, three clean worker results, and three ordered raw worker Docker-stats files; verifies exact 1,320 protocol/behavior paths, unique advertised hosts/endpoints, six distinct bounded Linux Docker Engine identities, exact 33 accepted worker IDs/incarnations/zones, per-domain 11-container raw-stat hashes and JSON rows, startup checks, timestamps, and one image digest without claiming physical independence or capacity | P0 | implemented with passing CLI coverage plus advertised-host/Engine-ID/incarnation reuse, state/image/resource tamper, malformed/incomplete/missing-resource, and non-F5-workload rejection; real six-engine artifact set pending | `python3 -m unittest tests.test_host_identity tests.test_f5_domain_preflight`; `python3 scripts/f5-domain-preflight.py --help` |
| F5 capacity verifier rejects incomplete policy criteria, weak workload/headroom/failure-domain declarations, dirty/failed/skipped runs, path escapes, missing artifacts, SHA-256 drift, and missing or mismatched passed F5 preflight and one-domain assignment reports | P0 | verifier binds the exact three-domain preflight report to the evidence workload and container image, plus the assignment report's canonical workload digest, baseline digest format, 22-worker/1,320/660/660 loss shape, and exact 440 ownership moves; approved run/evidence missing | `python3 -m unittest tests.test_scale_evidence`; `python3 -m videosim capacity-check --report <evidence.json> --policy scale/policies/f5-1000.json` |
| Durable control-plane load harness requires an empty disposable PostgreSQL database, creates the exact workload-driven worker/feed/lease shape, sustains heartbeats and profile-cadenced fenced reports, executes one declared worker-domain loss through real freshness expiry and production scheduling, fences a stale failed-owner report, commits synthetic endpoint-fault raise/recovery windows, records latency/resource/database metrics, and retains result plus alarm outbox evidence | P0 | exact one-tick, compressed loss, and 1,320-stream alarm-storm coverage implemented; the configured 30-second candidate default recovered all 440 affected streams onto 22 survivors at 29.888/29.889-second p95/p99, while the synthetic storm requires exact 1,320 raise/clear and 2,640 alarm-outbox transitions; real endpoint media faults and the 24-hour production-like run remain pending | `VIDEOSIM_TEST_POSTGRES_URL=... python3 -m unittest tests.test_control_plane_load tests.test_cli_control_plane_load tests.test_worker_v2`; `python3 -m videosim control-plane-load --database-url <url> --workload scale/workloads/f5-1000-candidate.json --duration 24h --worker-freshness-seconds <deployed-value> --output <report.json>` |
| Durable assignment evidence captures one read-only repeatable PostgreSQL snapshot and fails closed unless the F5 workload has exact desired/protocol coverage, fresh domain shape, advertised capacity, current single authority, balanced workers, tenant-matched baseline, and baseline-bounded ownership changes | P0 | implemented with exact synthetic and PostgreSQL-backed 1,320 baseline/one-domain-loss coverage; three-host capture pending | `python3 -m unittest tests.test_assignment_verifier tests.test_cli_assignment_verifier`; `python3 -m videosim verify-assignments --workload scale/workloads/f5-1000-candidate.json --output <capture.json> [--baseline <baseline.json>]` |
| Durable alarm evidence reconciles latest results, current check state, pending/current alarms, retained transition payloads, and transactional outbox records from one read-only repeatable PostgreSQL snapshot; inconclusive evidence preserves an active alarm and an unexplained clear fails closed | P0 | implemented with exact PostgreSQL-backed 1,320-stream projection and false-clear coverage; independent-host 24-hour capture pending | `VIDEOSIM_TEST_POSTGRES_URL=... python3 -m unittest tests.test_alarm_consistency tests.test_cli_alarm_consistency`; `python3 -m videosim verify-alarm-consistency --workload scale/workloads/f5-1000-candidate.json --output <report.json>` |
| Durable scheduler enforces total plus SRT/DASH admission tokens, evenly fills bounded workers, and limits the checked-in 1,320-stream candidate's repeated 11-worker domain losses to each failed domain's 440 assignments | P0 | implemented model plus local loss/rejoin/second-loss and same-process partition PostgreSQL smokes; multi-host/media/24-hour evidence pending | `python3 -m unittest tests.test_gui tests.test_scale_evidence tests.test_worker tests.test_cli_video_feed`; see `docs/f5-local-domain-loss-smoke.md` |
| Compose exposes a sample worker node service | P1 | implemented | `docker compose config --quiet` |
| Candidate worker-domain Compose renders 11 unique mTLS worker IDs with exact 60/30/30 admission and 12/2 probe limits, bounded encrypted spools, and hardened containers; startup validates candidate identities/certificates, immutable image resolution, bounded Docker Engine identity, host plus worker-path health APIs, exact accepted worker/incarnation registrations, stable processes, complete raw Docker CPU/memory/PID stats with a retained SHA-256, and Docker logs | P0 | render and host-local durable API startup passed with 11 registration records; immutable-image HTTPS/mTLS deployment on three independent hosts remains pending | `python3 -m unittest tests.test_worker_domain_compose tests.test_worker_domain_startup tests.test_cert_script`; `VIDEOSIM_WORKER_DOMAIN_STARTUP_INTEGRATION=1 python3 -m unittest tests.test_worker_domain_startup.WorkerDomainStartupIntegrationTest` |
| Candidate worker-domain physical stop waits for an exact 33-worker/1,320-assignment baseline, hard-stops 11 containers, requires all 440 affected authorities to recover within 45-second p95/90-second p99 while 880 healthy owners stay fixed, holds recovery, restarts/rejoins the domain, holds all 22 survivor incarnations stable for one freshness window, and rejects critical target Docker logs | P0 | implemented and marked on one Docker engine with retained 1,320-stream evidence at 36.233-second p95/37.315-second p99; API and peer-log audit passed, while independent hosts, HTTPS/mTLS, real media, measured survivor headroom, and 24-hour admission remain pending | `python3 -m unittest tests.test_worker_domain_fault`; `VIDEOSIM_WORKER_DOMAIN_FAULT_INTEGRATION=1 VIDEOSIM_WORKER_FAULT_STARTUP_RESULT=<result.json> VIDEOSIM_WORKER_FAULT_ARTIFACT_DIR=<dir> VIDEOSIM_TEST_POSTGRES_URL=<url> python3 -m unittest tests.test_worker_domain_fault.WorkerDomainFaultIntegrationTest` |
| Worker SIGINT/SIGTERM sends its final report before an incarnation-fenced drain and immediate reoffer | P0 | implemented; PostgreSQL integration gated | `python3 -m unittest tests.test_worker tests.test_worker_api`; `VIDEOSIM_TEST_POSTGRES_URL=... python3 -m unittest tests.test_postgres_store tests.test_worker_v2` |
| Critical validation runs before deep checks while deep checks defer on stable per-stream cadence offsets without false alarm clears | P0 | implemented | `python3 -m unittest tests.test_monitor tests.test_worker tests.test_cli_video_feed tests.test_gui` |
| Validation and deep-check phases enforce independent concurrency tokens through CLI, heartbeat, and worker execution | P0 | implemented | `python3 -m unittest tests.test_monitor tests.test_worker tests.test_cli_video_feed tests.test_gui` |
| Independent heartbeats publish schema-bounded cycle/deferral/spool/retry pressure and durable operator state exposes the latest worker snapshot | P0 | implemented unit/integration | `python3 -m unittest tests.test_worker tests.test_gui`; `VIDEOSIM_TEST_POSTGRES_URL=... python3 -m unittest tests.test_postgres_store` |
| Completed worker batches publish bounded CPU delta, process/child peak RSS, and Linux post-batch open descriptors through the existing pressure snapshot | P1 | implemented instrumentation | `python3 -m unittest tests.test_worker tests.test_gui` |
| Durable assignment excludes spool-blocked workers, preserves schedulable fresh owners within capacity during incomplete recovery, limits modeled 1,300-item worker loss to 130 required moves, rebalances on join, and recovers after DB-time worker/lease expiry with a higher epoch and stale-report rejection | P0 | implemented unit/integration plus repeated local loss and whole-domain transport-partition recovery; the partition recovered 440/440 failed leases in 61.194 seconds with zero healthy authority changes and same-process rejoin | `python3 -m unittest tests.test_gui tests.test_scale_evidence tests.test_worker`; `VIDEOSIM_TEST_POSTGRES_URL=... python3 -m unittest tests.test_postgres_store tests.test_worker_v2`; see `docs/f5-local-domain-loss-smoke.md` |
| Worker offer/renew and acknowledgement arrays use one ordered atomic PostgreSQL transaction and at most two SQL statements per request (worker fence plus set-based lease write); a late invalid tuple rolls back earlier lease changes | P0 | implemented integration with 1,000-row batch coverage | `VIDEOSIM_TEST_POSTGRES_URL=... python3 -m unittest tests.test_postgres_store tests.test_worker_v2` |
| Durable probe-report ingestion bulk-reads feed/lease/result/current-state fences and batch-writes current state, lease sequences, and event retention while retaining immutable per-result inserts and alarm transitions | P0 | implemented with a 60-stream/120-result at-most-140-SQL integration check and a local 1,320-stream fault follow-up with zero HTTP 499/5xx; sustained production saturation remains open | `VIDEOSIM_TEST_POSTGRES_URL=... python3 -m unittest tests.test_postgres_store`; see `docs/f5-local-domain-loss-smoke.md` |
| Durable assignment reconstructs the current external-feed catalog from locked PostgreSQL rows, so a replica started before create/update/delete mutations cannot assign from stale process state | P0 | implemented integration plus local two-API/1,320-feed smoke; generated-feed and operator-state replication remain open | `VIDEOSIM_TEST_POSTGRES_URL=... python3 -m unittest tests.test_postgres_store.PostgresControlPlaneIntegrationTest.test_scheduler_catalog_snapshot_locks_feed_versions tests.test_worker_v2.DurableWorkerV2ApiIntegrationTest.test_assignment_replica_reads_current_external_catalog`; see `docs/f5-local-domain-loss-smoke.md` |
| Durable assignment reads and lease writes share one tenant-scoped blocking PostgreSQL advisory-lock transaction; backend loss rolls back before a queued second store takes leadership | P0 | implemented integration and local two-API burst with zero contention retries; persistent leadership and failover SLO pending | `VIDEOSIM_TEST_POSTGRES_URL=... python3 -m unittest tests.test_postgres_store.PostgresControlPlaneIntegrationTest.test_scheduler_transaction_rolls_back_and_releases_after_connection_loss tests.test_worker_v2` |
| Versioned operator reads require viewer authorization, fail closed on invalid durable rows, bootstrap at most 100 feeds, page at most 200 catalog rows, omit unbounded stream/probe arrays from overview, and return one stream-scoped detail with explicit runtime knowledge | P0 | implemented integration, React pagination, and alternating two-API 1,320-feed/14-page smoke; snapshot cursors and generated-runtime ownership remain open | `python3 -m unittest tests.test_gui tests.test_worker_api tests.test_security`; `VIDEOSIM_TEST_POSTGRES_URL=... python3 -m unittest tests.test_worker_v2.DurableWorkerV2ApiIntegrationTest.test_operator_catalog_replica_pages_current_external_feeds tests.test_worker_v2.DurableWorkerV2ApiIntegrationTest.test_operator_catalog_fails_closed_on_invalid_durable_feed tests.test_worker_v2.DurableWorkerV2ApiIntegrationTest.test_operator_catalog_fails_closed_on_overlong_durable_feed_id`; see `docs/f5-local-domain-loss-smoke.md` |
| PostgreSQL API replicas start without preloading durable feed rows and allocate UUID-backed feed IDs; concurrent creates remain unique and a forced collision fails without overwriting the winner | P0 | implemented integration plus two post-seed API/1,320-feed restart and create smoke | `VIDEOSIM_TEST_POSTGRES_URL=... python3 -m unittest tests.test_worker_v2.DurableWorkerV2ApiIntegrationTest.test_durable_replicas_start_empty_and_create_collision_safe_ids`; see `docs/f5-local-domain-loss-smoke.md` |
| Generated SRT writes allocate one tenant-unique port from a bounded PostgreSQL range and fail closed at exhaustion | P0 | implemented with exact 1,000-port concurrent two-store coverage and two-API startup evidence | `VIDEOSIM_TEST_POSTGRES_URL=... python3 -m unittest tests.test_generated_runtime.GeneratedRuntimePostgresIntegrationTest.test_two_replicas_allocate_exact_1000_unique_srt_ports`; `VIDEOSIM_GENERATED_RUNTIME_INTEGRATION=1 python3 -m unittest tests.test_generated_runtime_startup` |
| Generated runtime takes a config-version ready lock only after child launch; any API detail reports current owner/session health and loses it on runtime death/stop | P0 | implemented PostgreSQL integration plus marked two-API kill/recreate/media workflow | `VIDEOSIM_TEST_POSTGRES_URL=... python3 -m unittest tests.test_generated_runtime.GeneratedRuntimePostgresIntegrationTest.test_session_lock_allows_one_owner_and_clean_takeover`; `VIDEOSIM_GENERATED_RUNTIME_INTEGRATION=1 python3 -m unittest tests.test_generated_runtime_startup` |
| Durable configuration forms require a positive client config version; any replica may mutate external configuration or generated update/start/fault/stop/delete intent, and stale writes return HTTP 409 without mutation | P0 | implemented integration plus local two-API external mutation and generated-runtime lifecycle smokes; idempotent create remains open | `VIDEOSIM_TEST_POSTGRES_URL=... python3 -m unittest tests.test_worker_v2.DurableWorkerV2ApiIntegrationTest.test_external_mutations_route_across_replicas_and_fence_stale_forms`; `VIDEOSIM_GENERATED_RUNTIME_INTEGRATION=1 python3 -m unittest tests.test_generated_runtime_startup` |
| Aggregate batch pressure queues at most one concurrency-sized validation window, stops new probe starts after exhaustion, preserves inconclusive alarms, and rotates deferred streams into the next cycle | P0 | implemented | `python3 -m unittest tests.test_monitor tests.test_worker tests.test_cli_video_feed tests.test_gui` |
| Worker v2 reports are encrypted and fsynced before send, replay before registration, remain byte-bounded, and block new probes during transport outage | P0 | implemented unit plus local whole-domain partition replay/fencing; multi-host production chaos pending | `python3 -m unittest tests.test_report_spool tests.test_worker tests.test_cli_video_feed`; see `docs/f5-local-domain-loss-smoke.md` |

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

SRT caption detection requires extracted caption bytes. Unit coverage rejects a
zero-byte successful receiver, and the live normal/video-only matrix proves
caption bytes while no-caption remains absent.

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
