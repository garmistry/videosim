from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from urllib.parse import urljoin
from urllib.request import urlopen
from xml.etree import ElementTree

from .feed import VideoFeedConfig
from .probe_deadline import probe_timeout


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
    if config.protocol == "dash":
        return _validate_dash(config, report)
    return _validate_srt(config, report)


def _validate_srt(config: VideoFeedConfig, report: ValidationReport) -> ValidationReport:
    if config.passive:
        report.video_present = _track_present(config.endpoint, "video")
        report.audio_present = _track_present(config.endpoint, "audio")
        report.captions_present = report.video_present and _captions_present(config.endpoint)
        report.reachable = report.video_present or report.audio_present or report.captions_present
        if not report.reachable:
            report.errors.append("feed unreachable or no streams detected")
        report.passed = not report.errors
        return report

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


def _validate_dash(config: VideoFeedConfig, report: ValidationReport) -> ValidationReport:
    manifest = _wait_for_dash_manifest(config)
    if not manifest:
        report.errors.append("DASH manifest unreachable")
        return report

    try:
        adaptations = _wait_for_dash_adaptations(config, manifest)
    except ElementTree.ParseError as exc:
        report.errors.append(f"DASH manifest parse failed: {exc}")
        return report

    video_segment = _wait_for_dash_segment(config, "video", manifest)
    audio_segment = _wait_for_dash_segment(config, "audio", manifest)
    report.video_present = "video" in adaptations and bool(video_segment)
    report.audio_present = "audio" in adaptations and bool(audio_segment)
    report.captions_present = "text" in adaptations and _dash_captions_present(config, manifest)
    if config.passive:
        report.reachable = report.video_present or report.audio_present or report.captions_present
        if not report.reachable:
            report.errors.append("DASH feed unreachable or no streams detected")
        report.passed = not report.errors
        return report

    report.reachable = (report.video_present if config.video else True) and (report.audio_present if config.audio else True)
    if not report.reachable:
        report.errors.append("DASH feed unreachable or expected segments missing")
        return report

    if config.video:
        if config.pattern == "black" and video_segment:
            frames = _read_dash_rgb_frames(config, video_segment, 3)
            report.black_video = _black_pixel_ratio(frames) >= 0.95
        if config.frozen and video_segment:
            frames = _read_dash_rgb_frames(config, video_segment, 3)
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
        timeout=probe_timeout(12),
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
    if len(result.stdout) < frame_size * count:
        raise RuntimeError("not enough decoded video frames")
    return result.stdout[: frame_size * count]


def _external_dash(config: VideoFeedConfig) -> bool:
    return bool(config.external_endpoint) and config.endpoint.startswith(("http://", "https://"))


def _wait_for_dash_manifest(config: VideoFeedConfig, timeout: float = 15) -> Path | bytes | None:
    wait_seconds = probe_timeout(timeout)
    budget_limited = wait_seconds < timeout
    if _external_dash(config):
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            data = _fetch_url(config.endpoint)
            if data:
                return data
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
        if budget_limited:
            raise TimeoutError("stream probe budget exhausted")
        return None
    manifest = Path(config.dash_dir) / config.dash_manifest
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if manifest.is_file() and manifest.stat().st_size > 0:
            return manifest
        time.sleep(min(0.25, max(0, deadline - time.monotonic())))
    if budget_limited:
        raise TimeoutError("stream probe budget exhausted")
    return None


def _dash_root(manifest: Path | bytes):
    return ElementTree.fromstring(manifest) if isinstance(manifest, bytes) else ElementTree.parse(manifest).getroot()


def _dash_adaptations(manifest: Path | bytes) -> set[str]:
    root = _dash_root(manifest)
    adaptations = set()
    for element in root.iter():
        if element.tag.endswith("AdaptationSet"):
            content_type = _adaptation_kind(element)
            if content_type:
                adaptations.add(content_type)
    return adaptations


def _wait_for_dash_adaptations(config: VideoFeedConfig, manifest: Path | bytes, timeout: float = 8) -> set[str]:
    wait_seconds = probe_timeout(timeout)
    budget_limited = wait_seconds < timeout
    deadline = time.monotonic() + wait_seconds
    adaptations = _dash_adaptations(manifest)
    while not isinstance(manifest, bytes) and config.captions and "text" not in adaptations and time.monotonic() < deadline:
        time.sleep(min(0.25, max(0, deadline - time.monotonic())))
        adaptations = _dash_adaptations(manifest)
    if budget_limited and time.monotonic() >= deadline:
        raise TimeoutError("stream probe budget exhausted")
    return adaptations


def _wait_for_dash_segment(config: VideoFeedConfig, kind: str, manifest: Path | bytes | None = None, timeout: float = 15) -> Path | bytes | None:
    wait_seconds = probe_timeout(timeout)
    budget_limited = wait_seconds < timeout
    if _external_dash(config):
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            source = manifest or _wait_for_dash_manifest(config, timeout=1)
            if source:
                try:
                    urls = _dash_segment_urls(source, config.endpoint, kind)
                except ElementTree.ParseError:
                    return None
                for url in urls:
                    data = _fetch_url(url)
                    if data:
                        return data
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
        if budget_limited:
            raise TimeoutError("stream probe budget exhausted")
        return None
    pattern = f"{kind}_0_*.ts"
    root = Path(config.dash_dir)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        segments = sorted(root.glob(pattern), key=lambda path: (path.stat().st_mtime, path.name))
        segments = [path for path in segments if path.stat().st_size > 0]
        if segments:
            if kind == "video" and len(segments) > 1:
                return segments[-2]
            return segments[-1]
        time.sleep(min(0.25, max(0, deadline - time.monotonic())))
    if budget_limited:
        raise TimeoutError("stream probe budget exhausted")
    return None


def _dash_captions_present(config: VideoFeedConfig, manifest: Path | bytes | None = None) -> bool:
    if _external_dash(config):
        source = manifest or _wait_for_dash_manifest(config, timeout=1)
        return bool(source and "text" in _dash_adaptations(source))
    caption_file = Path(config.dash_dir) / "captions.vtt"
    if not caption_file.is_file():
        return False
    probe_timeout(float("inf"))
    text = caption_file.read_text(encoding="utf-8", errors="replace")
    probe_timeout(float("inf"))
    return text.startswith("WEBVTT") and "VIDEOSIM" in text


def _read_dash_rgb_frames(config: VideoFeedConfig, segment: Path | bytes, count: int):
    frame_size = config.width * config.height * 3
    temp_path = None
    if isinstance(segment, bytes):
        probe_timeout(float("inf"))
        tmp = tempfile.NamedTemporaryFile(prefix="videosim-dash-segment-", suffix=".ts", delete=False)
        tmp.write(segment)
        tmp.close()
        temp_path = tmp.name
        segment = Path(temp_path)
    try:
        result = subprocess.run(
            [
                "gst-launch-1.0",
                "-q",
                "filesrc",
                f"location={segment}",
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
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=probe_timeout(12),
        )
    finally:
        if temp_path:
            Path(temp_path).unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
    if len(result.stdout) < frame_size * count:
        raise RuntimeError("not enough decoded video frames")
    return result.stdout[: frame_size * count]


def _receiver_succeeds(args, timeout):
    bounded_timeout = probe_timeout(timeout)
    try:
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=bounded_timeout)
    except subprocess.TimeoutExpired as exc:
        if bounded_timeout < timeout:
            raise TimeoutError("stream probe budget exhausted") from exc
        return False
    return result.returncode == 0


def _fetch_url(url: str, timeout: float = 8) -> bytes:
    bounded_timeout = probe_timeout(timeout)
    try:
        with urlopen(url, timeout=bounded_timeout) as response:
            data = response.read()
    except TimeoutError:
        if bounded_timeout < timeout:
            raise
        return b""
    except Exception:
        return b""
    probe_timeout(float("inf"))
    return data


def _adaptation_kind(element) -> str:
    content_type = element.attrib.get("contentType", "")
    if content_type in {"video", "audio", "text"}:
        return content_type
    mime_values = [element.attrib.get("mimeType", "")]
    mime_values.extend(child.attrib.get("mimeType", "") for child in element if child.tag.endswith("Representation"))
    codecs = " ".join([element.attrib.get("codecs", ""), *[child.attrib.get("codecs", "") for child in element if child.tag.endswith("Representation")]]).lower()
    for mime in (value.lower() for value in mime_values):
        if mime.startswith("video/"):
            return "video"
        if mime.startswith("audio/"):
            return "audio"
        if mime.startswith("text/") or mime == "application/ttml+xml" or (mime == "application/mp4" and "wvtt" in codecs):
            return "text"
    return ""


def _dash_segment_urls(manifest: Path | bytes, manifest_url: str, kind: str) -> list[str]:
    root = _dash_root(manifest)
    urls = []
    root_base = _node_base_url(manifest_url, root)
    periods = _children(root, "Period") or [root]
    for period in periods:
        period_base = _node_base_url(root_base, period)
        for adaptation in _children(period, "AdaptationSet"):
            if _adaptation_kind(adaptation) != kind:
                continue
            adaptation_base = _node_base_url(period_base, adaptation)
            representations = _children(adaptation, "Representation") or [adaptation]
            for representation in representations:
                rep_base = _node_base_url(adaptation_base, representation)
                urls.extend(_segment_list_urls(representation, adaptation, rep_base))
                urls.extend(_segment_template_urls(representation, adaptation, rep_base))
    return urls


def _children(element, suffix: str) -> list:
    return [child for child in list(element) if child.tag.endswith(suffix)]


def _first_child(element, suffix: str):
    return next((child for child in list(element) if child.tag.endswith(suffix)), None)


def _node_base_url(base: str, element) -> str:
    node = _first_child(element, "BaseURL")
    if node is not None and node.text and node.text.strip():
        return urljoin(base, node.text.strip())
    return base


def _segment_list_urls(representation, adaptation, base_url: str) -> list[str]:
    segment_list = _first_child(representation, "SegmentList") or _first_child(adaptation, "SegmentList")
    if segment_list is None:
        return []
    return [urljoin(base_url, segment.attrib["media"]) for segment in _children(segment_list, "SegmentURL") if segment.attrib.get("media")]


def _segment_template_urls(representation, adaptation, base_url: str) -> list[str]:
    template = _first_child(representation, "SegmentTemplate") or _first_child(adaptation, "SegmentTemplate")
    if template is None or not template.attrib.get("media"):
        return []
    try:
        start_number = int(template.attrib.get("startNumber", "1"))
    except ValueError:
        start_number = 1
    media = _template_media(template.attrib["media"], representation.attrib.get("id", ""), start_number)
    return [urljoin(base_url, media)]


def _template_media(media: str, representation_id: str, number: int) -> str:
    media = media.replace("$RepresentationID$", representation_id)
    media = re.sub(r"\$Number(?:%0(\d+)d)?\$", lambda match: str(number).zfill(int(match.group(1) or 0)), media)
    media = re.sub(r"\$Time(?:%0(\d+)d)?\$", lambda match: "0".zfill(int(match.group(1) or 0)), media)
    return media


def _black_pixel_ratio(frames):
    black = sum(1 for byte in frames if byte < 16)
    return black / len(frames)


def _near_identical_ratio(first, second):
    close = sum(1 for a, b in zip(first, second) if abs(a - b) < 8)
    return close / min(len(first), len(second))
