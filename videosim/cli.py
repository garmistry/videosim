from __future__ import annotations

import argparse
import shlex
import sys
from dataclasses import replace

from .feed import FeedError, VideoFeedConfig, run_video_feed, video_pipeline_args
from .gui import GuiState, run_gui
from .monitor import DEFAULT_MONITOR_STATE_PATH, run_monitor
from .profile import ProfileError, load_profile
from .soak import check_reports
from .soak import check_summary
from .soak import gui_summary
from .soak import human_summary as soak_summary
from .soak import run_gui_soak
from .soak import run_soak
from .validator import human_summary, validate_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="videosim")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="start a synthetic video feed")
    start.add_argument("--profile", help="load feed settings from a flat YAML profile")
    start.add_argument("--protocol", choices=["srt", "dash"], help="feed protocol")
    start.add_argument("--port", type=int)
    start.add_argument("--width", type=int)
    start.add_argument("--height", type=int)
    start.add_argument("--framerate", type=int)
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
    validate.add_argument("--framerate", type=int, help="override the profile framerate")
    validate.add_argument("--dash-dir", help="directory containing DASH MPD and media segments")
    validate.add_argument("--dash-base-url", help="base URL used when reporting DASH endpoint")
    validate.add_argument("--json", action="store_true", help="print machine-readable JSON")

    monitor = subparsers.add_parser("monitor", help="poll GUI feeds and write monitor alarms/events")
    monitor.add_argument("--gui-state-url", default="http://127.0.0.1:8080/state.json")
    monitor.add_argument("--state-path", default=DEFAULT_MONITOR_STATE_PATH)
    monitor.add_argument("--poll-interval-seconds", type=float, default=5)
    monitor.add_argument("--repeat-interval-seconds", type=float, default=5)
    monitor.add_argument("--history-limit", type=int, default=1000)
    monitor.add_argument("--srt-host", default="127.0.0.1")
    monitor.add_argument("--once", action="store_true", help="poll once and exit")

    gui = subparsers.add_parser("gui", help="launch the local browser GUI")
    gui.add_argument("--host", default="127.0.0.1")
    gui.add_argument("--http-port", type=int, default=8080)
    gui.add_argument("--feed-port", type=int, default=9000)
    gui.add_argument("--width", type=int, default=1280)
    gui.add_argument("--height", type=int, default=720)
    gui.add_argument("--framerate", type=int, default=30)

    soak = subparsers.add_parser("soak", help="run a timed feed soak with periodic validation")
    soak.add_argument("--profile", required=True, help="profile describing expected stream state")
    soak.add_argument("--port", type=int, default=9000)
    soak.add_argument("--width", type=int, default=1280)
    soak.add_argument("--height", type=int, default=720)
    soak.add_argument("--framerate", type=int, default=30)
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
    gui_soak.add_argument("--framerate", type=int, default=30)
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
    except (FeedError, ProfileError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())
