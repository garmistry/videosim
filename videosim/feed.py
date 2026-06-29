from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import shlex
import signal
import subprocess
import threading
import time


class FeedError(Exception):
    pass


SRT_CALLER_LIMIT = "unbounded-by-gstreamer-srtsink"
DASH_MANIFEST = "manifest.mpd"
DASH_SEGMENT_DURATION_SECONDS = 2
DASH_CAPTION_FILE = "captions.vtt"


@dataclass(frozen=True)
class VideoFeedConfig:
    protocol: str = "srt"
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
    dash_dir: str = "/tmp/videosim-dash"
    dash_base_url: str = ""
    dash_manifest: str = DASH_MANIFEST

    def __post_init__(self):
        for name in ("port", "width", "height", "framerate", "audio_frequency"):
            value = getattr(self, name)
            if value < 1:
                raise ValueError(f"{name} must be greater than 0")
        if self.protocol not in {"srt", "dash"}:
            raise ValueError("protocol must be srt or dash")
        if self.port > 65535:
            raise ValueError("port must be between 1 and 65535")
        if self.captions and not self.video:
            raise ValueError("captions require video")
        if not self.video and not self.audio:
            raise ValueError("at least one of video or audio must be enabled")

    @property
    def endpoint(self) -> str:
        if self.protocol == "dash":
            if self.dash_base_url:
                return f"{self.dash_base_url.rstrip('/')}/{self.dash_manifest}"
            return f"file://{Path(self.dash_dir).resolve()}/{self.dash_manifest}"
        return f"srt://127.0.0.1:{self.port}?mode=caller"


def require_gst_launch() -> str:
    gst_launch = shutil.which("gst-launch-1.0")
    if not gst_launch:
        raise FeedError("Missing gst-launch-1.0. Run scripts/install-deps.sh.")
    return gst_launch


def verbose_enabled() -> bool:
    return os.environ.get("VIDEOSIM_VERBOSE", "").lower() in {"1", "true", "yes", "on"}


def video_pipeline_args(config: VideoFeedConfig) -> list[str]:
    if config.protocol == "dash":
        return dash_pipeline_args(config)
    return srt_pipeline_args(config)


def srt_pipeline_args(config: VideoFeedConfig) -> list[str]:
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
                "clockoverlay",
                "halignment=right",
                "valignment=top",
                "shaded-background=true",
                'time-format=%Y-%m-%d %H:%M:%S',
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


def dash_pipeline_args(config: VideoFeedConfig) -> list[str]:
    args = [
        require_gst_launch(),
        "-e",
        "dashsink",
        "name=dash",
        "dynamic=true",
        f"target-duration={DASH_SEGMENT_DURATION_SECONDS}",
        f"mpd-root-path={Path(config.dash_dir)}",
        f"mpd-filename={config.dash_manifest}",
        "muxer=ts",
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
                "clockoverlay",
                "halignment=right",
                "valignment=top",
                "shaded-background=true",
                'time-format=%Y-%m-%d %H:%M:%S',
                "!",
            ]
        )
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
        args.extend(["dash.video_0"])
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
                "dash.audio_0",
            ]
        )
    return args


def run_video_feed(config: VideoFeedConfig) -> int:
    if config.protocol == "dash":
        prepare_dash_dir(config)
    args = video_pipeline_args(config)
    print(f"Starting {config.protocol.upper()} video feed at {config.endpoint}", flush=True)
    if verbose_enabled():
        print(
            "Feed config: "
            f"protocol={config.protocol} port={config.port} size={config.width}x{config.height} framerate={config.framerate} "
            f"video={config.video} audio={config.audio} captions={config.captions} "
            f"pattern={config.pattern} frozen={config.frozen} dash_dir={config.dash_dir} "
            f"srt_caller_limit={SRT_CALLER_LIMIT}",
            flush=True,
        )
        print(f"GStreamer command: {shlex.join(args)}", flush=True)
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE if config.protocol == "srt" and config.captions and config.video else None,
    )
    if verbose_enabled():
        print(f"{config.protocol.upper()} feed subprocess pid={proc.pid}", flush=True)
    writer = None
    if config.protocol == "dash" and config.captions and config.video:
        writer = threading.Thread(target=write_dash_caption_sidecar, args=(config,), daemon=True)
        writer.start()
    elif config.captions and proc.stdin:
        writer = threading.Thread(target=write_caption_stream, args=(proc.stdin, config.framerate), daemon=True)
        writer.start()

    try:
        return proc.wait()
    except KeyboardInterrupt:
        print(f"Stopping {config.protocol.upper()} video feed", flush=True)
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


def prepare_dash_dir(config: VideoFeedConfig):
    dash_dir = Path(config.dash_dir)
    dash_dir.mkdir(parents=True, exist_ok=True)
    for path in dash_dir.iterdir():
        if path.is_file() and (
            path.name in {config.dash_manifest, DASH_CAPTION_FILE} or path.suffix in {".ts", ".m4s", ".mp4"}
        ):
            path.unlink()


def write_dash_caption_sidecar(config: VideoFeedConfig):
    dash_dir = Path(config.dash_dir)
    sequence = 0
    while True:
        caption_path = dash_dir / DASH_CAPTION_FILE
        caption_path.write_text(_dash_caption_vtt(sequence), encoding="utf-8")
        sequence += 1
        _ensure_dash_caption_manifest(config)
        time.sleep(1)


def _dash_caption_vtt(sequence: int) -> str:
    return (
        "WEBVTT\n\n"
        "00:00:00.000 --> 23:59:59.000\n"
        f"VIDEOSIM DASH CAPTIONS {sequence:04d}\n"
    )


def _ensure_dash_caption_manifest(config: VideoFeedConfig):
    manifest = Path(config.dash_dir) / config.dash_manifest
    if not manifest.is_file():
        return
    text = manifest.read_text(encoding="utf-8", errors="replace")
    if DASH_CAPTION_FILE in text or "</Period>" not in text:
        return
    adaptation = (
        '<AdaptationSet id="caption_0" contentType="text" mimeType="text/vtt" lang="en">'
        '<Representation id="caption_0" bandwidth="256">'
        f"<BaseURL>{DASH_CAPTION_FILE}</BaseURL>"
        "</Representation>"
        "</AdaptationSet>"
    )
    manifest.write_text(text.replace("</Period>", f"{adaptation}</Period>"), encoding="utf-8")


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
