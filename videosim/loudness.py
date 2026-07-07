from __future__ import annotations

import math
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .feed import VideoFeedConfig


EBU_R128_TARGET_LUFS = -23.0
EBU_R128_TOLERANCE_LU = 1.0
EBU_R128_TRUE_PEAK_MAX_DBTP = -1.0
ATSC_A85_TARGET_LKFS = -24.0
ATSC_A85_TOLERANCE_LU = 2.0


class LoudnessError(Exception):
    pass


@dataclass(frozen=True)
class LoudnessReport:
    integrated_lufs: float
    true_peak_dbtp: float
    source: str


def measure_loudness(config: VideoFeedConfig, sample_seconds: float = 5.0) -> LoudnessReport:
    input_args, source = _input_args(config, sample_seconds)
    args = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-nostats",
        *input_args,
        "-vn",
        "-filter_complex",
        "ebur128=peak=true",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(10, sample_seconds + 6),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LoudnessError(f"loudness probe failed: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").splitlines()[-1:] or ["ffmpeg loudness probe failed"]
        raise LoudnessError(detail[0])
    return parse_ebur128_summary(result.stderr.decode("utf-8", "replace"), source)


def parse_ebur128_summary(text: str, source: str = "") -> LoudnessReport:
    summary = text.rsplit("Summary:", 1)[-1]
    integrated = _number_after(r"\bI:\s*([+-]?(?:inf|nan|\d+(?:\.\d+)?))\s+LUFS", summary)
    peak = _number_after(r"\bPeak:\s*([+-]?(?:inf|nan|\d+(?:\.\d+)?))\s+dB(?:FS|TP)", summary)
    if integrated is None or peak is None or not math.isfinite(integrated) or math.isnan(peak):
        raise LoudnessError("ffmpeg ebur128 summary did not include finite integrated loudness and true peak")
    return LoudnessReport(integrated_lufs=integrated, true_peak_dbtp=peak, source=source)


def _number_after(pattern: str, text: str) -> float | None:
    match = re.search(pattern, text, re.IGNORECASE)
    return float(match.group(1)) if match else None


def _input_args(config: VideoFeedConfig, sample_seconds: float) -> tuple[list[str], str]:
    if config.protocol == "dash":
        segment = _latest_dash_audio_segment(config)
        return ["-i", str(segment)], str(segment)
    return ["-t", str(sample_seconds), "-i", config.endpoint], config.endpoint


def _latest_dash_audio_segment(config: VideoFeedConfig) -> Path:
    paths = sorted(Path(config.dash_dir).glob("audio_0_*.ts"), key=lambda path: path.stat().st_mtime)
    if not paths:
        raise LoudnessError("no DASH audio segment available for loudness measurement")
    return paths[-1]
