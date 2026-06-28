from __future__ import annotations

from dataclasses import dataclass
import shutil
import signal
import subprocess


class FeedError(Exception):
    pass


@dataclass(frozen=True)
class VideoFeedConfig:
    port: int = 9000
    width: int = 1280
    height: int = 720
    framerate: int = 30
    pattern: str = "smpte"

    def __post_init__(self):
        for name in ("port", "width", "height", "framerate"):
            value = getattr(self, name)
            if value < 1:
                raise ValueError(f"{name} must be greater than 0")
        if self.port > 65535:
            raise ValueError("port must be between 1 and 65535")

    @property
    def endpoint(self) -> str:
        return f"srt://127.0.0.1:{self.port}?mode=caller"


def require_gst_launch() -> str:
    gst_launch = shutil.which("gst-launch-1.0")
    if not gst_launch:
        raise FeedError("Missing gst-launch-1.0. Run scripts/install-deps.sh.")
    return gst_launch


def video_pipeline_args(config: VideoFeedConfig) -> list[str]:
    return [
        require_gst_launch(),
        "-e",
        "videotestsrc",
        "is-live=true",
        f"pattern={config.pattern}",
        "!",
        f"video/x-raw,width={config.width},height={config.height},framerate={config.framerate}/1",
        "!",
        "x264enc",
        "tune=zerolatency",
        "speed-preset=ultrafast",
        f"key-int-max={config.framerate}",
        "!",
        "h264parse",
        "!",
        "mpegtsmux",
        "!",
        "srtsink",
        f'uri=srt://:{config.port}?mode=listener',
    ]


def run_video_feed(config: VideoFeedConfig) -> int:
    args = video_pipeline_args(config)
    print(f"Starting SRT video feed at {config.endpoint}", flush=True)
    proc = subprocess.Popen(args)

    try:
        return proc.wait()
    except KeyboardInterrupt:
        print("Stopping SRT video feed", flush=True)
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=10)
        return 0
