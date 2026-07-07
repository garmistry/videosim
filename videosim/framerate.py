from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .feed import VideoFeedConfig


SUPPORTED_FRAME_RATES = (
    ("23.97", "24000/1001"),
    ("24", "24/1"),
    ("25", "25/1"),
    ("50", "50/1"),
    ("59.94", "60000/1001"),
    ("60", "60/1"),
)
FRAME_RATE_TOLERANCE_FPS = 0.15


class FrameRateError(Exception):
    pass


@dataclass(frozen=True)
class FrameRateReport:
    measured_fps: float
    source: str


def supported_frame_rate_options() -> list[dict[str, str]]:
    return [{"value": value, "label": f"{value} fps"} for value, _fraction in SUPPORTED_FRAME_RATES]


def normalize_frame_rate(value) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError("framerate must be greater than 0")
    for label, fraction in SUPPORTED_FRAME_RATES:
        if text == label or text == fraction:
            return label
    try:
        numeric = float(Fraction(text))
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError("framerate must be a positive number") from exc
    if numeric <= 0:
        raise ValueError("framerate must be greater than 0")
    for label, fraction in SUPPORTED_FRAME_RATES:
        if abs(numeric - float(Fraction(fraction))) <= FRAME_RATE_TOLERANCE_FPS:
            return label
    return str(int(numeric)) if numeric.is_integer() else text


def frame_rate_fraction(value) -> str:
    normalized = normalize_frame_rate(value)
    for label, fraction in SUPPORTED_FRAME_RATES:
        if normalized == label:
            return fraction
    fraction = Fraction(normalized).limit_denominator(1001)
    return f"{fraction.numerator}/{fraction.denominator}"


def frame_rate_float(value) -> float:
    return float(Fraction(frame_rate_fraction(value)))


def frame_rate_keyint(value) -> int:
    return max(1, round(frame_rate_float(value)))


def measure_frame_rate(config: "VideoFeedConfig") -> FrameRateReport:
    input_path = _frame_rate_input(config)
    args = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=avg_frame_rate,r_frame_rate",
        "-of",
        "json",
        str(input_path),
    ]
    try:
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=12)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FrameRateError(f"frame-rate probe failed: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.splitlines()[-1:] or ["ffprobe frame-rate probe failed"]
        raise FrameRateError(detail[0])
    return FrameRateReport(parse_ffprobe_frame_rate(result.stdout), str(input_path))


def parse_ffprobe_frame_rate(text: str) -> float:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FrameRateError("ffprobe frame-rate output was not JSON") from exc
    for stream in payload.get("streams", []):
        for key in ("avg_frame_rate", "r_frame_rate"):
            value = stream.get(key)
            if value and value != "0/0":
                fps = float(Fraction(value))
                if fps > 0:
                    return fps
    raise FrameRateError("ffprobe did not report a video frame rate")


def _frame_rate_input(config: "VideoFeedConfig") -> str | Path:
    if config.protocol == "dash" and config.external_endpoint:
        return config.endpoint
    if config.protocol == "dash":
        paths = sorted(Path(config.dash_dir).glob("video_0_*.ts"), key=lambda path: path.stat().st_mtime)
        if not paths:
            raise FrameRateError("no DASH video segment available for frame-rate measurement")
        return paths[-1]
    return config.endpoint
