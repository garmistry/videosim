from __future__ import annotations

import argparse
import shlex
import sys
from dataclasses import replace

from .feed import FeedError, VideoFeedConfig, run_video_feed, video_pipeline_args
from .profile import ProfileError, load_profile
from .validator import human_summary, validate_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="videosim")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="start a synthetic SRT video feed")
    start.add_argument("--profile", help="load feed settings from a flat YAML profile")
    start.add_argument("--port", type=int)
    start.add_argument("--width", type=int)
    start.add_argument("--height", type=int)
    start.add_argument("--framerate", type=int)
    start.add_argument("--pattern")
    start.add_argument("--no-audio", action="store_true", help="disable generated audio")
    start.add_argument("--audio-frequency", type=int, help="generated audio tone frequency in Hz")
    start.add_argument("--no-captions", action="store_true", help="disable generated CEA-608 captions")
    start.add_argument("--print-command", action="store_true", help="print GStreamer command and exit")

    validate = subparsers.add_parser("validate", help="validate a running SRT feed against a profile")
    validate.add_argument("--profile", required=True, help="profile describing expected stream state")
    validate.add_argument("--port", type=int, help="override the profile port")
    validate.add_argument("--width", type=int, help="override the profile width")
    validate.add_argument("--height", type=int, help="override the profile height")
    validate.add_argument("--framerate", type=int, help="override the profile framerate")
    validate.add_argument("--json", action="store_true", help="print machine-readable JSON")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "validate":
            config = load_profile(args.profile)
            overrides = {
                key: value
                for key, value in {
                    "port": args.port,
                    "width": args.width,
                    "height": args.height,
                    "framerate": args.framerate,
                }.items()
                if value is not None
            }
            if overrides:
                config = replace(config, **overrides)
            report = validate_config(config)
            print(report.to_json() if args.json else human_summary(report))
            return 0 if report.passed else 1

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
