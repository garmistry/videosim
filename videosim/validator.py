from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass, field

from .feed import VideoFeedConfig


@dataclass
class ValidationReport:
    endpoint: str
    reachable: bool = False
    video_present: bool = False
    audio_present: bool = False
    captions_present: bool = False
    black_video: bool = False
    frozen_video: bool = False
    passed: bool = False
    errors: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


def validate_config(config: VideoFeedConfig) -> ValidationReport:
    report = ValidationReport(endpoint=config.endpoint)
    expected = {"video": config.video, "audio": config.audio, "captions": config.captions}
    report.reachable = _expected_tracks_present(config.endpoint, expected)
    if not report.reachable:
        report.errors.append("feed unreachable or expected streams missing")
        return report

    if config.video:
        report.video_present = True
    else:
        report.video_present = _track_present(config.endpoint, "video")

    if config.audio:
        report.audio_present = True
    else:
        report.audio_present = _track_present(config.endpoint, "audio")

    if config.captions:
        report.captions_present = True
    else:
        report.captions_present = _captions_present(config.endpoint)

    if config.pattern == "black":
        frames = _read_rgb_frames(config, 3)
        report.black_video = _black_pixel_ratio(frames) >= 0.95
    if config.frozen:
        frames = _read_rgb_frames(config, 3)
        frame_size = config.width * config.height * 3
        report.frozen_video = _near_identical_ratio(frames[:frame_size], frames[-frame_size:]) >= 0.95

    _check_expectations(config, report)
    report.passed = not report.errors
    return report


def human_summary(report: ValidationReport) -> str:
    status = "PASS" if report.passed else "FAIL"
    fields = [
        f"Validation {status}: {report.endpoint}",
        f"reachable={report.reachable}",
        f"video_present={report.video_present}",
        f"audio_present={report.audio_present}",
        f"captions_present={report.captions_present}",
        f"black_video={report.black_video}",
        f"frozen_video={report.frozen_video}",
    ]
    if report.errors:
        fields.append("errors=" + "; ".join(report.errors))
    return "\n".join(fields)


def _check_expectations(config: VideoFeedConfig, report: ValidationReport):
    if report.video_present != config.video:
        report.errors.append(f"video_present expected {config.video} got {report.video_present}")
    if report.audio_present != config.audio:
        report.errors.append(f"audio_present expected {config.audio} got {report.audio_present}")
    if report.captions_present != config.captions:
        report.errors.append(f"captions_present expected {config.captions} got {report.captions_present}")
    if config.pattern == "black" and not report.black_video:
        report.errors.append("black video not detected")
    if config.frozen and not report.frozen_video:
        report.errors.append("frozen video not detected")


def _expected_tracks_present(endpoint, expected):
    args = [
        "gst-launch-1.0",
        "-q",
        "srtsrc",
        f"uri={endpoint}",
        "!",
        "tsdemux",
        "name=demux",
    ]
    if expected["video"]:
        args.extend(["demux.", "!", "queue", "!", "h264parse", "!"])
        if expected["captions"]:
            args.extend(
                [
                    "tee",
                    "name=video",
                    "video.",
                    "!",
                    "queue",
                    "!",
                    "fakesink",
                    "sync=false",
                    "num-buffers=5",
                    "video.",
                    "!",
                    "queue",
                    "!",
                    "h264ccextractor",
                    "!",
                    "fakesink",
                    "sync=false",
                    "num-buffers=3",
                ]
            )
        else:
            args.extend(["fakesink", "sync=false", "num-buffers=5"])
    if expected["audio"]:
        args.extend(["demux.", "!", "queue", "!", "aacparse", "!", "fakesink", "sync=false", "num-buffers=5"])
    return _receiver_succeeds(args, timeout=15)


def _track_present(endpoint, track):
    parser = "h264parse" if track == "video" else "aacparse"
    return _receiver_succeeds(
        [
            "gst-launch-1.0",
            "-q",
            "srtsrc",
            f"uri={endpoint}",
            "!",
            "tsdemux",
            "name=demux",
            "demux.",
            "!",
            "queue",
            "!",
            parser,
            "!",
            "fakesink",
            "sync=false",
            "num-buffers=5",
        ],
        timeout=8,
    )


def _captions_present(endpoint):
    return _receiver_succeeds(
        [
            "gst-launch-1.0",
            "-q",
            "srtsrc",
            f"uri={endpoint}",
            "!",
            "tsdemux",
            "name=demux",
            "demux.",
            "!",
            "queue",
            "!",
            "h264parse",
            "!",
            "h264ccextractor",
            "!",
            "fakesink",
            "sync=false",
            "num-buffers=3",
        ],
        timeout=8,
    )


def _read_rgb_frames(config: VideoFeedConfig, count: int):
    frame_size = config.width * config.height * 3
    result = subprocess.run(
        [
            "gst-launch-1.0",
            "-q",
            "srtsrc",
            f"uri={config.endpoint}",
            "!",
            "tsdemux",
            "name=demux",
            "demux.",
            "!",
            "queue",
            "!",
            "h264parse",
            "!",
            "avdec_h264",
            "!",
            "videoconvert",
            "!",
            f"video/x-raw,format=RGB,width={config.width},height={config.height}",
            "!",
            "identity",
            f"eos-after={count}",
            "!",
            "fdsink",
            "fd=1",
        ]
        + (
            ["demux.", "!", "queue", "!", "aacparse", "!", "fakesink", "sync=false", "num-buffers=5"]
            if config.audio
            else []
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=12,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
    if len(result.stdout) < frame_size * count:
        raise RuntimeError("not enough decoded video frames")
    return result.stdout[: frame_size * count]


def _receiver_succeeds(args, timeout):
    try:
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def _black_pixel_ratio(frames):
    black = sum(1 for byte in frames if byte < 16)
    return black / len(frames)


def _near_identical_ratio(first, second):
    close = sum(1 for a, b in zip(first, second) if abs(a - b) < 8)
    return close / min(len(first), len(second))
