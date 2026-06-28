from __future__ import annotations

from dataclasses import dataclass
import shutil
import signal
import subprocess
import threading
import time


class FeedError(Exception):
    pass


@dataclass(frozen=True)
class VideoFeedConfig:
    port: int = 9000
    width: int = 1280
    height: int = 720
    framerate: int = 30
    pattern: str = "smpte"
    video: bool = True
    audio: bool = True
    audio_frequency: int = 440
    captions: bool = True
    frozen: bool = False

    def __post_init__(self):
        for name in ("port", "width", "height", "framerate", "audio_frequency"):
            value = getattr(self, name)
            if value < 1:
                raise ValueError(f"{name} must be greater than 0")
        if self.port > 65535:
            raise ValueError("port must be between 1 and 65535")
        if self.captions and not self.video:
            raise ValueError("captions require video")
        if not self.video and not self.audio:
            raise ValueError("at least one of video or audio must be enabled")

    @property
    def endpoint(self) -> str:
        return f"srt://127.0.0.1:{self.port}?mode=caller"


def require_gst_launch() -> str:
    gst_launch = shutil.which("gst-launch-1.0")
    if not gst_launch:
        raise FeedError("Missing gst-launch-1.0. Run scripts/install-deps.sh.")
    return gst_launch


def video_pipeline_args(config: VideoFeedConfig) -> list[str]:
    args = [
        require_gst_launch(),
        "-e",
        "mpegtsmux",
        "name=mux",
        "!",
        "srtsink",
        f"uri=srt://:{config.port}?mode=listener",
    ]
    if config.video:
        args.extend(["videotestsrc", "is-live=true", f"pattern={config.pattern}"])
        if config.frozen:
            args.extend(["num-buffers=1", "!", "imagefreeze", "is-live=true"])
        args.extend(
            [
                "!",
                f"video/x-raw,width={config.width},height={config.height},framerate={config.framerate}/1",
                "!",
            ]
        )
        if config.captions:
            args.extend(["cccombiner", "name=cc", "!", f"video/x-raw,framerate={config.framerate}/1", "!"])
        args.extend(
            [
                "x264enc",
                "tune=zerolatency",
                "speed-preset=ultrafast",
                f"key-int-max={config.framerate}",
                "!",
                "h264parse",
                "!",
                "video/x-h264,alignment=au",
                "!",
            ]
        )
        if config.captions:
            args.extend(["h264ccinserter", "!", "h264parse", "!"])
        args.extend(["mux."])
    if config.audio:
        args.extend(
            [
                "audiotestsrc",
                "is-live=true",
                "wave=sine",
                f"freq={config.audio_frequency}",
                "!",
                "audio/x-raw,rate=48000,channels=2",
                "!",
                "avenc_aac",
                "!",
                "aacparse",
                "!",
                "mux.",
            ]
        )
    if config.captions and config.video:
        args.extend(
            [
                "fdsrc",
                "fd=0",
                "do-timestamp=true",
                "!",
                f"closedcaption/x-cea-608,format=raw,field=0,framerate={config.framerate}/1",
                "!",
                "cc.caption",
            ]
        )
    return args


def run_video_feed(config: VideoFeedConfig) -> int:
    args = video_pipeline_args(config)
    print(f"Starting SRT video feed at {config.endpoint}", flush=True)
    proc = subprocess.Popen(args, stdin=subprocess.PIPE if config.captions and config.video else None)
    writer = None
    if config.captions and proc.stdin:
        writer = threading.Thread(target=write_caption_stream, args=(proc.stdin, config.framerate), daemon=True)
        writer.start()

    try:
        return proc.wait()
    except KeyboardInterrupt:
        print("Stopping SRT video feed", flush=True)
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        stdin = getattr(proc, "stdin", None)
        if stdin:
            stdin.close()
        if writer:
            writer.join(timeout=1)
        return 0


def write_caption_stream(stream, framerate: int):
    delay = 1 / (framerate * 5)
    sequence = 0
    # ponytail: fixed startup delay; use bus-driven negotiation if sub-second captions matter.
    time.sleep(3)
    while True:
        text = f"VIDEOSIM {sequence:04d} "
        sequence += 1
        for pair in cea608_pairs(text):
            try:
                stream.write(pair)
                stream.flush()
            except (BrokenPipeError, ValueError):
                return
            time.sleep(delay)


def cea608_pairs(text: str):
    encoded = [_odd_parity(ord(char) & 0x7F) for char in text]
    if len(encoded) % 2:
        encoded.append(_odd_parity(ord(" ")))
    for index in range(0, len(encoded), 2):
        yield bytes(encoded[index : index + 2])


def _odd_parity(value: int) -> int:
    value &= 0x7F
    if value.bit_count() % 2 == 0:
        return value | 0x80
    return value
