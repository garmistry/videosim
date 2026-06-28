from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


REQUIRED_SOAK_REPORTS = ("normal", "audio_only", "video_only", "no_captions", "black_video", "frozen_video")


@dataclass
class SoakReport:
    profile: str
    endpoint: str
    duration_seconds: float
    validation_interval_seconds: float
    validations: int = 0
    crashes: int = 0
    memory_start_mb: float | None = None
    memory_end_mb: float | None = None
    memory_growth_mb: float | None = None
    passed: bool = False
    errors: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


@dataclass
class SoakCheckReport:
    report_dir: str
    memory_growth_threshold_mb: float
    checked_reports: int = 0
    passed: bool = False
    errors: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


def run_soak(
    profile: str,
    port: int,
    width: int,
    height: int,
    framerate: int,
    duration_seconds: float,
    validation_interval_seconds: float,
    startup_seconds: float = 4,
) -> SoakReport:
    endpoint = f"srt://127.0.0.1:{port}?mode=caller"
    report = SoakReport(profile, endpoint, duration_seconds, validation_interval_seconds)
    start_time = time.monotonic()
    deadline = start_time + duration_seconds
    sender = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "videosim",
            "start",
            "--profile",
            profile,
            "--port",
            str(port),
            "--width",
            str(width),
            "--height",
            str(height),
            "--framerate",
            str(framerate),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        if startup_seconds > 0:
            time.sleep(min(startup_seconds, max(0, deadline - time.monotonic())))
        report.memory_start_mb = sample_rss_mb(sender.pid)
        while time.monotonic() < deadline:
            if sender.poll() is not None:
                report.crashes += 1
                output = sender.stdout.read() if sender.stdout else ""
                report.errors.append(f"sender exited early with {sender.returncode}: {output}".strip())
                break
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "videosim",
                    "validate",
                    "--profile",
                    profile,
                    "--port",
                    str(port),
                    "--width",
                    str(width),
                    "--height",
                    str(height),
                    "--framerate",
                    str(framerate),
                    "--json",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=60,
            )
            report.validations += 1
            if result.returncode != 0:
                report.errors.append(result.stdout.strip() or "validation failed")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(validation_interval_seconds, remaining))
        report.memory_end_mb = sample_rss_mb(sender.pid)
        if report.memory_start_mb is not None and report.memory_end_mb is not None:
            report.memory_growth_mb = round(report.memory_end_mb - report.memory_start_mb, 3)
    finally:
        stop_sender(sender)
    report.passed = report.crashes == 0 and report.validations > 0 and not report.errors
    return report


def stop_sender(sender):
    if sender.poll() is None:
        os.killpg(sender.pid, signal.SIGINT)
        try:
            sender.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(sender.pid, signal.SIGKILL)
            sender.wait(timeout=5)
    if sender.stdout:
        sender.stdout.close()


def sample_rss_mb(pid: int) -> float | None:
    proc_status = f"/proc/{pid}/status"
    try:
        with open(proc_status, encoding="utf-8") as status:
            for line in status:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 3)
    except OSError:
        pass

    try:
        result = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], stdout=subprocess.PIPE, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return round(int(value) / 1024, 3) if result.returncode == 0 and value else None


def human_summary(report: SoakReport) -> str:
    status = "PASS" if report.passed else "FAIL"
    fields = [
        f"Soak {status}: {report.profile}",
        f"endpoint={report.endpoint}",
        f"duration_seconds={report.duration_seconds}",
        f"validation_interval_seconds={report.validation_interval_seconds}",
        f"validations={report.validations}",
        f"crashes={report.crashes}",
        f"memory_start_mb={report.memory_start_mb}",
        f"memory_end_mb={report.memory_end_mb}",
        f"memory_growth_mb={report.memory_growth_mb}",
    ]
    if report.errors:
        fields.append("errors=" + "; ".join(report.errors))
    return "\n".join(fields)


def check_reports(report_dir: str, memory_growth_threshold_mb: float = 200) -> SoakCheckReport:
    check = SoakCheckReport(report_dir, memory_growth_threshold_mb)
    base = Path(report_dir)
    for name in REQUIRED_SOAK_REPORTS:
        path = base / f"{name}.json"
        if not path.is_file():
            check.errors.append(f"missing report: {path}")
            continue
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            check.errors.append(f"invalid report {path}: {exc}")
            continue
        check.checked_reports += 1
        if not report.get("passed"):
            check.errors.append(f"{name} did not pass")
        if report.get("crashes") != 0:
            check.errors.append(f"{name} crashes={report.get('crashes')}")
        if report.get("validations", 0) < 1:
            check.errors.append(f"{name} has no validations")
        growth = report.get("memory_growth_mb")
        if growth is not None and growth > memory_growth_threshold_mb:
            check.errors.append(f"{name} memory_growth_mb={growth} exceeds {memory_growth_threshold_mb}")
    check.passed = not check.errors
    return check


def check_summary(report: SoakCheckReport) -> str:
    status = "PASS" if report.passed else "FAIL"
    fields = [
        f"Soak check {status}: {report.report_dir}",
        f"checked_reports={report.checked_reports}",
        f"memory_growth_threshold_mb={report.memory_growth_threshold_mb}",
    ]
    if report.errors:
        fields.append("errors=" + "; ".join(report.errors))
    return "\n".join(fields)
