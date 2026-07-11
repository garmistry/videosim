from __future__ import annotations

import argparse
import shlex
import sys
from dataclasses import replace

from .distributed_benchmark import (
    BenchmarkInvariantError,
    human_summary as control_plane_benchmark_summary,
    run_control_plane_benchmark,
)
from .feed import FeedError, VideoFeedConfig, run_video_feed, video_pipeline_args
from .feed_store import SqliteFeedStore, default_feed_db_path
from .gui import GuiState, run_gui
from .monitor import DEFAULT_MONITOR_STATE_PATH, run_monitor
from .profile import ProfileError, load_profile
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
                    feed_store=SqliteFeedStore(default_feed_db_path()),
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
            if not args.worker_id.strip():
                raise ValueError("worker_id is required")
            ssl_context = build_ssl_context(args.tls_ca_file, args.tls_cert_file, args.tls_key_file)
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
    except (BenchmarkInvariantError, FeedError, ProfileError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())
