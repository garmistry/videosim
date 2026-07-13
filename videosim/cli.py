from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import sys
import time
import uuid
from dataclasses import replace

from .distributed_benchmark import (
    BenchmarkInvariantError,
    human_summary as control_plane_benchmark_summary,
    run_control_plane_benchmark,
)
from .feed import FeedError, VideoFeedConfig, run_video_feed, video_pipeline_args
from .feed_store import SqliteFeedStore, configured_database_url, default_feed_db_path, default_feed_store
from .gui import GuiState, run_gui
from .migrations import MigrationError, PostgresMigrator
from .monitor import DEFAULT_MONITOR_STATE_PATH, run_monitor
from .nats_publisher import NatsPublisherError, ensure_event_stream, run_outbox_publisher
from .postgres_store import PostgresControlPlaneStore, PostgresStoreError
from .profile import ProfileError, load_profile
from .report_spool import ReportSpoolError
from .scale_evidence import check_scale_evidence
from .scale_evidence import human_summary as scale_evidence_summary
from .security import SecurityConfig
from .soak import check_reports
from .soak import check_summary
from .soak import gui_summary
from .soak import human_summary as soak_summary
from .soak import run_gui_soak
from .soak import run_soak
from .validator import human_summary, validate_config
from .worker import build_ssl_context, default_worker_id, run_worker


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="videosim")
    subparsers = parser.add_subparsers(dest="command", required=True)

    migrate = subparsers.add_parser("migrate", help="apply checksum-verified PostgreSQL migrations")
    migrate.add_argument("--database-url", default="")

    migration_status = subparsers.add_parser("migration-status", help="show PostgreSQL migration status")
    migration_status.add_argument("--database-url", default="")
    migration_status.add_argument("--json", action="store_true")

    import_sqlite = subparsers.add_parser("import-sqlite-feeds", help="idempotently import SQLite feed definitions into PostgreSQL")
    import_sqlite.add_argument("--database-url", default="")
    import_sqlite.add_argument("--sqlite-path", default=str(default_feed_db_path()))

    nats_init = subparsers.add_parser("nats-init", help="provision the VideoSim JetStream event stream")
    nats_init.add_argument("--nats-url", default="nats://127.0.0.1:4222")

    outbox = subparsers.add_parser("outbox-publisher", help="publish committed PostgreSQL outbox events to JetStream")
    outbox.add_argument("--database-url", default="")
    outbox.add_argument("--nats-url", default="nats://127.0.0.1:4222")
    outbox.add_argument("--poll-interval-seconds", type=float, default=1)
    outbox.add_argument("--batch-size", type=int, default=100)
    outbox.add_argument("--publisher-id", default="")
    outbox.add_argument("--once", action="store_true")

    outbox_requeue = subparsers.add_parser("outbox-requeue", help="requeue one inspected dead outbox event")
    outbox_requeue.add_argument("--database-url", default="")
    outbox_requeue.add_argument("--event-id", required=True)

    monitor_history_prune = subparsers.add_parser(
        "monitor-history-prune",
        help="prune age-expired durable monitor event history",
    )
    monitor_history_prune.add_argument("--database-url", default="")
    monitor_history_prune.add_argument("--poll-interval-seconds", type=float, default=3600)
    monitor_history_prune.add_argument("--batch-size", type=int, default=1000)
    monitor_history_prune.add_argument("--once", action="store_true")

    start = subparsers.add_parser("start", help="start a synthetic video feed")
    start.add_argument("--profile", help="load feed settings from a flat YAML profile")
    start.add_argument("--protocol", choices=["srt", "dash"], help="feed protocol")
    start.add_argument("--port", type=int)
    start.add_argument("--width", type=int)
    start.add_argument("--height", type=int)
    start.add_argument("--framerate")
    start.add_argument("--pattern")
    start.add_argument("--no-audio", action="store_true", help="disable generated audio")
    start.add_argument("--audio-frequency", type=int, help="generated audio tone frequency in Hz")
    start.add_argument("--no-captions", action="store_true", help="disable generated CEA-608 captions")
    start.add_argument("--dash-dir", help="directory for DASH MPD and media segments")
    start.add_argument("--dash-base-url", help="base URL used when printing DASH endpoint")
    start.add_argument("--print-command", action="store_true", help="print GStreamer command and exit")

    validate = subparsers.add_parser("validate", help="validate a running feed against a profile")
    validate.add_argument("--profile", required=True, help="profile describing expected stream state")
    validate.add_argument("--protocol", choices=["srt", "dash"], help="override the profile protocol")
    validate.add_argument("--port", type=int, help="override the profile port")
    validate.add_argument("--width", type=int, help="override the profile width")
    validate.add_argument("--height", type=int, help="override the profile height")
    validate.add_argument("--framerate", help="override the profile framerate")
    validate.add_argument("--dash-dir", help="directory containing DASH MPD and media segments")
    validate.add_argument("--dash-base-url", help="base URL used when reporting DASH endpoint")
    validate.add_argument("--endpoint", help="external SRT or DASH endpoint URL to validate")
    validate.add_argument("--json", action="store_true", help="print machine-readable JSON")

    monitor = subparsers.add_parser("monitor", help="poll GUI feeds and write monitor alarms/events")
    monitor.add_argument("--gui-state-url", default="http://127.0.0.1:8080/state.json")
    monitor.add_argument("--state-path", default=DEFAULT_MONITOR_STATE_PATH)
    monitor.add_argument("--poll-interval-seconds", type=float, default=5)
    monitor.add_argument("--repeat-interval-seconds", type=float, default=5)
    monitor.add_argument("--history-limit", type=int, default=1000)
    monitor.add_argument("--srt-host", default="127.0.0.1")
    monitor.add_argument("--once", action="store_true", help="poll once and exit")

    worker = subparsers.add_parser("worker", help="poll a master control plane and monitor assigned feeds")
    worker.add_argument("--control-plane-url", default="http://127.0.0.1:8080")
    worker.add_argument("--worker-id", default=default_worker_id())
    worker.add_argument("--poll-interval-seconds", type=float, default=5)
    worker.add_argument("--repeat-interval-seconds", type=float, default=5)
    worker.add_argument("--history-limit", type=int, default=1000)
    worker.add_argument("--srt-host", default="127.0.0.1")
    worker.add_argument("--heartbeat-interval-seconds", type=float, default=20)
    worker.add_argument("--tls-ca-file", default="")
    worker.add_argument("--tls-cert-file", default="")
    worker.add_argument("--tls-key-file", default="")
    worker.add_argument("--retry-attempts", type=int, default=5)
    worker.add_argument("--retry-base-seconds", type=float, default=0.25)
    worker.add_argument(
        "--max-streams",
        type=int,
        default=0,
        help="durable scheduler admission limit; zero keeps legacy unlimited assignment",
    )
    worker.add_argument(
        "--max-srt-streams",
        type=int,
        default=0,
        help="durable SRT admission limit; zero leaves this protocol uncapped",
    )
    worker.add_argument(
        "--max-dash-streams",
        type=int,
        default=0,
        help="durable DASH admission limit; zero leaves this protocol uncapped",
    )
    worker.add_argument(
        "--max-concurrent-checks",
        type=int,
        default=1,
        help="bounded per-worker stream probe concurrency",
    )
    worker.add_argument(
        "--max-concurrent-deep-checks",
        type=int,
        default=0,
        help="deep-phase stream concurrency; zero inherits --max-concurrent-checks",
    )
    worker.add_argument(
        "--stream-budget-seconds",
        type=float,
        default=0,
        help="per-stream probe budget; zero disables budget-based deferral",
    )
    worker.add_argument(
        "--deep-check-interval-seconds",
        type=float,
        default=0,
        help="staggered TR-101/frame-rate/loudness cadence; zero runs every cycle",
    )
    worker.add_argument(
        "--batch-budget-seconds",
        type=float,
        default=0,
        help="defer deep checks when validation consumes this batch budget; zero disables",
    )
    worker.add_argument("--report-spool-dir", default="")
    worker.add_argument("--report-spool-key-file", default="")
    worker.add_argument("--report-spool-max-bytes", type=int, default=0)
    worker.add_argument("--once", action="store_true", help="poll once and exit")

    control_plane_benchmark = subparsers.add_parser(
        "control-plane-benchmark",
        help="benchmark in-process assignment/report integrity without media probes",
    )
    control_plane_benchmark.add_argument("--streams", type=int, default=1000)
    control_plane_benchmark.add_argument("--workers", type=int, default=10)
    control_plane_benchmark.add_argument("--iterations", type=int, default=5)
    control_plane_benchmark.add_argument("--warmup-iterations", type=int, default=1)
    control_plane_benchmark.add_argument("--seed", type=int, default=1)
    control_plane_benchmark.add_argument("--json", action="store_true", help="print machine-readable JSON")

    capacity_check = subparsers.add_parser(
        "capacity-check",
        help="fail closed unless a scale-evidence bundle satisfies its policy",
    )
    capacity_check.add_argument("--report", required=True)
    capacity_check.add_argument("--policy", required=True)
    capacity_check.add_argument("--json", action="store_true", help="print machine-readable JSON")

    gui = subparsers.add_parser("gui", help="launch the local browser GUI")
    gui.add_argument("--host", default="127.0.0.1")
    gui.add_argument("--http-port", type=int, default=8080)
    gui.add_argument("--feed-port", type=int, default=9000)
    gui.add_argument("--width", type=int, default=1280)
    gui.add_argument("--height", type=int, default=720)
    gui.add_argument("--framerate", default="59.94")
    gui.add_argument("--worker-base-url", default="", help="externally reachable control-plane origin used in worker assignments")
    gui.add_argument(
        "--allow-legacy-worker-reports",
        action="store_true",
        help="temporarily accept unversioned worker reports with ownership scoping but no stale-generation fence",
    )

    soak = subparsers.add_parser("soak", help="run a timed feed soak with periodic validation")
    soak.add_argument("--profile", required=True, help="profile describing expected stream state")
    soak.add_argument("--port", type=int, default=9000)
    soak.add_argument("--width", type=int, default=1280)
    soak.add_argument("--height", type=int, default=720)
    soak.add_argument("--framerate", default=30)
    soak.add_argument("--duration-seconds", type=float, default=60)
    soak.add_argument("--validation-interval-seconds", type=float, default=15)
    soak.add_argument("--startup-seconds", type=float, default=4)
    soak.add_argument("--json", action="store_true", help="print machine-readable JSON")

    soak_check = subparsers.add_parser("soak-check", help="check soak JSON reports")
    soak_check.add_argument("--report-dir", required=True, help="directory containing normal.json and outage reports")
    soak_check.add_argument("--memory-growth-threshold-mb", type=float, default=200)
    soak_check.add_argument("--json", action="store_true", help="print machine-readable JSON")

    gui_soak = subparsers.add_parser("gui-soak", help="run a timed GUI responsiveness soak")
    gui_soak.add_argument("--host", default="127.0.0.1")
    gui_soak.add_argument("--http-port", type=int, default=8080)
    gui_soak.add_argument("--feed-port", type=int, default=9000)
    gui_soak.add_argument("--width", type=int, default=1280)
    gui_soak.add_argument("--height", type=int, default=720)
    gui_soak.add_argument("--framerate", default="59.94")
    gui_soak.add_argument("--duration-seconds", type=float, default=60)
    gui_soak.add_argument("--validation-interval-seconds", type=float, default=15)
    gui_soak.add_argument("--poll-interval-seconds", type=float, default=5)
    gui_soak.add_argument("--json", action="store_true", help="print machine-readable JSON")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command in {"migrate", "migration-status"}:
            database_url = args.database_url or configured_database_url()
            if not database_url:
                raise ValueError("PostgreSQL database URL is required")
            migrator = PostgresMigrator(database_url)
            states = migrator.apply() if args.command == "migrate" else migrator.status()
            payload = [
                {
                    "version": state.version,
                    "name": state.name,
                    "checksum": state.checksum,
                    "applied": state.applied,
                }
                for state in states
            ]
            if args.command == "migration-status" and args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                for state in payload:
                    print(f"{state['version']:03d} {state['name']}: {'applied' if state['applied'] else 'pending'}")
            return 0 if all(state["applied"] for state in payload) else 1

        if args.command == "import-sqlite-feeds":
            database_url = args.database_url or configured_database_url()
            if not database_url:
                raise ValueError("PostgreSQL database URL is required")
            target = PostgresControlPlaneStore(database_url)
            try:
                feeds = SqliteFeedStore(args.sqlite_path).load()
                changed = sum(target.import_feed_if_changed(feed)[0] for feed in feeds)
            finally:
                target.close()
            print(f"Imported {changed} changed feed definitions; inspected {len(feeds)}")
            return 0

        if args.command == "nats-init":
            asyncio.run(ensure_event_stream(args.nats_url))
            print("JetStream event stream ready")
            return 0

        if args.command == "outbox-publisher":
            database_url = args.database_url or configured_database_url()
            if not database_url:
                raise ValueError("PostgreSQL database URL is required")
            return run_outbox_publisher(
                database_url,
                args.nats_url,
                once=args.once,
                poll_seconds=args.poll_interval_seconds,
                batch_size=args.batch_size,
                publisher_id=args.publisher_id,
            )

        if args.command == "outbox-requeue":
            database_url = args.database_url or configured_database_url()
            if not database_url:
                raise ValueError("PostgreSQL database URL is required")
            event_id = uuid.UUID(args.event_id)
            store = PostgresControlPlaneStore(database_url)
            try:
                requeued = store.requeue_dead_outbox(event_id)
            finally:
                store.close()
            print(f"{'Requeued' if requeued else 'Did not requeue'} outbox event {event_id}")
            return 0 if requeued else 1

        if args.command == "monitor-history-prune":
            database_url = args.database_url or configured_database_url()
            if not database_url:
                raise ValueError("PostgreSQL database URL is required")
            if args.poll_interval_seconds <= 0:
                raise ValueError("poll interval must be greater than 0")
            store = PostgresControlPlaneStore(database_url)
            try:
                while True:
                    deleted = 0
                    while True:
                        batch = store.prune_expired_alarm_events(
                            batch_size=args.batch_size
                        )
                        deleted += batch
                        if batch < args.batch_size:
                            break
                    print(f"Pruned {deleted} expired monitor event rows")
                    if args.once:
                        return 0
                    time.sleep(args.poll_interval_seconds)
            finally:
                store.close()

        if args.command == "gui":
            run_gui(
                args.host,
                args.http_port,
                GuiState(
                    http_port=args.http_port,
                    feed_port=args.feed_port,
                    width=args.width,
                    height=args.height,
                    framerate=args.framerate,
                    worker_base_url=args.worker_base_url,
                    feed_store=default_feed_store(),
                    allow_legacy_worker_reports=args.allow_legacy_worker_reports,
                    security=SecurityConfig.from_env(),
                ),
            )
            return 0

        if args.command == "validate":
            config = load_profile(args.profile)
            overrides = {
                key: value
                for key, value in {
                    "port": args.port,
                    "width": args.width,
                    "height": args.height,
                    "framerate": args.framerate,
                    "protocol": args.protocol,
                    "dash_dir": args.dash_dir,
                    "dash_base_url": args.dash_base_url,
                    "external_endpoint": args.endpoint,
                }.items()
                if value is not None
            }
            if overrides:
                config = replace(config, **overrides)
            report = validate_config(config)
            print(report.to_json() if args.json else human_summary(report))
            return 0 if report.passed else 1

        if args.command == "soak":
            if args.duration_seconds <= 0:
                raise ValueError("duration_seconds must be greater than 0")
            if args.validation_interval_seconds <= 0:
                raise ValueError("validation_interval_seconds must be greater than 0")
            if args.startup_seconds < 0:
                raise ValueError("startup_seconds must be zero or greater")
            report = run_soak(
                args.profile,
                args.port,
                args.width,
                args.height,
                args.framerate,
                args.duration_seconds,
                args.validation_interval_seconds,
                args.startup_seconds,
            )
            print(report.to_json() if args.json else soak_summary(report))
            return 0 if report.passed else 1

        if args.command == "soak-check":
            if args.memory_growth_threshold_mb < 0:
                raise ValueError("memory_growth_threshold_mb must be zero or greater")
            report = check_reports(args.report_dir, args.memory_growth_threshold_mb)
            print(report.to_json() if args.json else check_summary(report))
            return 0 if report.passed else 1

        if args.command == "gui-soak":
            if args.duration_seconds <= 0:
                raise ValueError("duration_seconds must be greater than 0")
            if args.validation_interval_seconds <= 0:
                raise ValueError("validation_interval_seconds must be greater than 0")
            if args.poll_interval_seconds <= 0:
                raise ValueError("poll_interval_seconds must be greater than 0")
            report = run_gui_soak(
                args.host,
                args.http_port,
                args.feed_port,
                args.width,
                args.height,
                args.framerate,
                args.duration_seconds,
                args.validation_interval_seconds,
                args.poll_interval_seconds,
            )
            print(report.to_json() if args.json else gui_summary(report))
            return 0 if report.passed else 1

        if args.command == "monitor":
            if args.poll_interval_seconds <= 0:
                raise ValueError("poll_interval_seconds must be greater than 0")
            if args.repeat_interval_seconds <= 0:
                raise ValueError("repeat_interval_seconds must be greater than 0")
            if args.history_limit < 1:
                raise ValueError("history_limit must be greater than 0")
            return run_monitor(
                args.gui_state_url,
                args.state_path,
                args.poll_interval_seconds,
                args.repeat_interval_seconds,
                args.history_limit,
                args.srt_host,
                args.once,
            )

        if args.command == "control-plane-benchmark":
            report = run_control_plane_benchmark(
                args.streams,
                args.workers,
                args.iterations,
                args.warmup_iterations,
                args.seed,
            )
            print(report.to_json() if args.json else control_plane_benchmark_summary(report))
            return 0 if report.passed else 1

        if args.command == "capacity-check":
            report = check_scale_evidence(args.report, args.policy)
            print(report.to_json() if args.json else scale_evidence_summary(report))
            return 0 if report.passed else 1

        if args.command == "worker":
            if args.poll_interval_seconds <= 0:
                raise ValueError("poll_interval_seconds must be greater than 0")
            if args.repeat_interval_seconds <= 0:
                raise ValueError("repeat_interval_seconds must be greater than 0")
            if args.history_limit < 1:
                raise ValueError("history_limit must be greater than 0")
            if args.heartbeat_interval_seconds <= 0:
                raise ValueError("heartbeat_interval_seconds must be greater than 0")
            if args.retry_attempts < 1:
                raise ValueError("retry_attempts must be at least 1")
            if args.retry_base_seconds < 0:
                raise ValueError("retry_base_seconds must be zero or greater")
            if args.max_streams < 0:
                raise ValueError("max_streams must be zero or greater")
            if args.max_srt_streams < 0:
                raise ValueError("max_srt_streams must be zero or greater")
            if args.max_dash_streams < 0:
                raise ValueError("max_dash_streams must be zero or greater")
            if args.max_concurrent_checks < 1:
                raise ValueError("max_concurrent_checks must be at least 1")
            if args.max_concurrent_deep_checks < 0:
                raise ValueError("max_concurrent_deep_checks must be zero or greater")
            if args.stream_budget_seconds < 0:
                raise ValueError("stream_budget_seconds must be zero or greater")
            if args.deep_check_interval_seconds < 0:
                raise ValueError("deep_check_interval_seconds must be zero or greater")
            if args.batch_budget_seconds < 0:
                raise ValueError("batch_budget_seconds must be zero or greater")
            spool_enabled = any(
                (
                    args.report_spool_dir.strip(),
                    args.report_spool_key_file.strip(),
                    args.report_spool_max_bytes,
                )
            )
            if spool_enabled and not (
                args.report_spool_dir.strip()
                and args.report_spool_key_file.strip()
                and args.report_spool_max_bytes > 0
            ):
                raise ValueError(
                    "report spool directory, key file, and positive max bytes are required together"
                )
            if not args.worker_id.strip():
                raise ValueError("worker_id is required")
            ssl_context = build_ssl_context(args.tls_ca_file, args.tls_cert_file, args.tls_key_file)
            if (
                args.max_streams
                or args.max_srt_streams
                or args.max_dash_streams
                or args.max_concurrent_checks > 1
                or args.max_concurrent_deep_checks
                or args.stream_budget_seconds
                or args.deep_check_interval_seconds
                or args.batch_budget_seconds
                or spool_enabled
            ):
                return run_worker(
                    args.control_plane_url,
                    args.worker_id.strip(),
                    args.poll_interval_seconds,
                    args.repeat_interval_seconds,
                    args.history_limit,
                    args.srt_host,
                    args.once,
                    args.heartbeat_interval_seconds,
                    ssl_context,
                    args.retry_attempts,
                    args.retry_base_seconds,
                    max_streams=args.max_streams,
                    max_srt_streams=args.max_srt_streams,
                    max_dash_streams=args.max_dash_streams,
                    max_concurrent_checks=args.max_concurrent_checks,
                    max_concurrent_deep_checks=args.max_concurrent_deep_checks,
                    stream_budget_seconds=args.stream_budget_seconds,
                    deep_check_interval_seconds=args.deep_check_interval_seconds,
                    batch_budget_seconds=args.batch_budget_seconds,
                    report_spool_dir=args.report_spool_dir.strip(),
                    report_spool_key_file=args.report_spool_key_file.strip(),
                    report_spool_max_bytes=args.report_spool_max_bytes,
                )
            return run_worker(
                args.control_plane_url,
                args.worker_id.strip(),
                args.poll_interval_seconds,
                args.repeat_interval_seconds,
                args.history_limit,
                args.srt_host,
                args.once,
                args.heartbeat_interval_seconds,
                ssl_context,
                args.retry_attempts,
                args.retry_base_seconds,
            )

        config = load_profile(args.profile) if args.profile else VideoFeedConfig()
        overrides = {
            key: value
            for key, value in {
                "port": args.port,
                "width": args.width,
                "height": args.height,
                "framerate": args.framerate,
                "pattern": args.pattern,
                "audio_frequency": args.audio_frequency,
                "protocol": args.protocol,
                "dash_dir": args.dash_dir,
                "dash_base_url": args.dash_base_url,
            }.items()
            if value is not None
        }
        if args.no_audio:
            overrides["audio"] = False
        if args.no_captions:
            overrides["captions"] = False
        if overrides:
            config = replace(config, **overrides)
        if args.print_command:
            print(shlex.join(video_pipeline_args(config)))
            print(config.endpoint)
            return 0
        return run_video_feed(config)
    except (
        BenchmarkInvariantError,
        FeedError,
        MigrationError,
        NatsPublisherError,
        PostgresStoreError,
        ProfileError,
        ReportSpoolError,
        ValueError,
    ) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())
