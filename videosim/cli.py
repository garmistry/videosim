from __future__ import annotations

import argparse
import shlex
import sys

from .feed import FeedError, VideoFeedConfig, run_video_feed, video_pipeline_args


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="videosim")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="start a synthetic SRT video feed")
    start.add_argument("--port", type=int, default=9000)
    start.add_argument("--width", type=int, default=1280)
    start.add_argument("--height", type=int, default=720)
    start.add_argument("--framerate", type=int, default=30)
    start.add_argument("--pattern", default="smpte")
    start.add_argument("--no-audio", action="store_true", help="disable generated audio")
    start.add_argument("--audio-frequency", type=int, default=440, help="generated audio tone frequency in Hz")
    start.add_argument("--no-captions", action="store_true", help="disable generated CEA-608 captions")
    start.add_argument("--print-command", action="store_true", help="print GStreamer command and exit")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = VideoFeedConfig(
            port=args.port,
            width=args.width,
            height=args.height,
            framerate=args.framerate,
            pattern=args.pattern,
            audio=not args.no_audio,
            audio_frequency=args.audio_frequency,
            captions=not args.no_captions,
        )
        if args.print_command:
            print(shlex.join(video_pipeline_args(config)))
            print(config.endpoint)
            return 0
        return run_video_feed(config)
    except (FeedError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())
