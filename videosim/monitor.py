from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse
from urllib.request import urlopen

from .alert_profile import alert_profile_from_stream
from .feed import DASH_SEGMENT_DURATION_SECONDS, VideoFeedConfig
from .framerate import FRAME_RATE_TOLERANCE_FPS, FrameRateError, frame_rate_float, measure_frame_rate
from .gui import controls_for_mode, profile_for
from .loudness import (
    ATSC_A85_TARGET_LKFS,
    ATSC_A85_TOLERANCE_LU,
    EBU_R128_TARGET_LUFS,
    EBU_R128_TOLERANCE_LU,
    EBU_R128_TRUE_PEAK_MAX_DBTP,
    LoudnessError,
    measure_loudness,
)
from .monitor_catalog import SPEC_BY_ID, monitor_catalog_payload
from .profile import load_profile
from .tr101 import analyze_ts
from .validator import ValidationReport, validate_config


DEFAULT_MONITOR_STATE_PATH = "/tmp/videosim-monitor/state.json"


@dataclass(frozen=True)
class MonitorIssue:
    stream_id: str
    stream_name: str
    monitor_id: str
    monitor_name: str
    severity: str
    message: str

    @property
    def alarm_id(self) -> str:
        return f"{self.stream_id}:{self.monitor_id}"

def empty_monitor_state() -> dict:
    return {"updatedAt": "", "alarms": [], "events": [], "pending": [], "monitors": monitor_catalog_payload()}


def load_monitor_state(path: str | Path) -> dict:
    file_path = Path(path)
    if not file_path.is_file():
        return empty_monitor_state()
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty_monitor_state()
    payload.setdefault("alarms", [])
    payload.setdefault("events", [])
    payload.setdefault("pending", [])
    payload["monitors"] = monitor_catalog_payload()
    return payload


def write_monitor_state(path: str | Path, state: dict):
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = file_path.with_suffix(file_path.suffix + ".tmp")
    temp_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    temp_path.replace(file_path)


def fetch_gui_state(url: str) -> dict:
    with urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def config_for_stream(stream: dict, srt_host: str) -> VideoFeedConfig:
    config = load_profile(profile_for(stream["protocol"], stream["mode"]))
    overrides = {
        key: stream[key]
        for key in ("width", "height", "framerate")
        if key in stream and stream[key] not in (None, "")
    }
    if stream.get("source") == "external":
        return replace(config, **overrides, protocol=stream["protocol"], external_endpoint=stream["endpoint"])
    if stream["protocol"] == "dash":
        return VideoFeedConfig(
            **{
                **config.__dict__,
                **overrides,
                "dash_dir": f"/tmp/videosim-dash/{stream['id']}",
                "dash_base_url": stream["endpoint"].rsplit("/", 1)[0],
            }
        )
    parsed = urlparse(stream["endpoint"])
    return VideoFeedConfig(**{**config.__dict__, **overrides, "srt_host": srt_host, "port": parsed.port or config.port})


def issues_for_report(stream: dict, report: ValidationReport) -> list[MonitorIssue]:
    controls = controls_for_mode(stream["mode"])
    profile = alert_profile_from_stream(stream)
    explicit = set(profile["enabledMonitorIds"] or []) if profile["enabledMonitorIds"] is not None else set()
    expected = lambda control, monitor_id: controls[control] or monitor_id in explicit
    issues = []
    if not report.reachable:
        issues.append(issue(stream, "feed_reachable", "Feed is unreachable or expected streams are missing"))
    if expected("video", "essence_video_present") and not report.video_present:
        issues.append(issue(stream, "essence_video_present", "Expected video is absent"))
    if expected("audio", "essence_audio_present") and not report.audio_present:
        issues.append(issue(stream, "essence_audio_present", "Expected audio is absent"))
    if expected("captions", "essence_captions_present") and not report.captions_present:
        issues.append(issue(stream, "essence_captions_present", "Expected captions are absent"))
    if expected("black_video", "black_video_detected") and not report.black_video:
        issues.append(issue(stream, "black_video_detected", "Black-video profile did not validate"))
    if expected("frozen_video", "frozen_video_detected") and not report.frozen_video:
        issues.append(issue(stream, "frozen_video_detected", "Frozen-video profile did not validate"))
    return issues


def tr101_issues_for_stream(stream: dict, config: VideoFeedConfig, sample_seconds: float = 1.0) -> list[MonitorIssue]:
    data, duration = ts_sample(config, sample_seconds)
    if not data:
        return []
    report = analyze_ts(data, duration)
    return [
        issue(stream, indicator, report.messages.get(indicator, SPEC_BY_ID[indicator].description))
        for indicator, active in report.indicators.items()
        if active
    ]


def loudness_issues_for_stream(stream: dict, config: VideoFeedConfig, sample_seconds: float = 5.0) -> list[MonitorIssue]:
    if not controls_for_mode(stream["mode"])["audio"]:
        return []
    try:
        report = measure_loudness(config, sample_seconds)
    except LoudnessError as exc:
        return [issue(stream, "loudness_bs1770_measurement", f"BS.1770 loudness measurement failed: {exc}")]

    issues = []
    ebu_delta = report.integrated_lufs - EBU_R128_TARGET_LUFS
    if abs(ebu_delta) > EBU_R128_TOLERANCE_LU:
        issues.append(
            issue(
                stream,
                "loudness_ebu_r128_integrated",
                f"Integrated loudness {report.integrated_lufs:.1f} LUFS is {ebu_delta:+.1f} LU from EBU R 128 target {EBU_R128_TARGET_LUFS:.1f} LUFS",
            )
        )
    if report.true_peak_dbtp > EBU_R128_TRUE_PEAK_MAX_DBTP:
        issues.append(
            issue(
                stream,
                "loudness_ebu_r128_true_peak",
                f"True peak {report.true_peak_dbtp:.1f} dBTP exceeds EBU R 128 maximum {EBU_R128_TRUE_PEAK_MAX_DBTP:.1f} dBTP",
            )
        )
    atsc_delta = report.integrated_lufs - ATSC_A85_TARGET_LKFS
    if abs(atsc_delta) > ATSC_A85_TOLERANCE_LU:
        issues.append(
            issue(
                stream,
                "loudness_atsc_a85_integrated",
                f"Integrated loudness {report.integrated_lufs:.1f} LKFS is {atsc_delta:+.1f} LU from ATSC A/85 target {ATSC_A85_TARGET_LKFS:.1f} LKFS",
            )
        )
    return issues


def frame_rate_issues_for_stream(stream: dict, config: VideoFeedConfig) -> list[MonitorIssue]:
    if not controls_for_mode(stream["mode"])["video"]:
        return []
    expected = frame_rate_float(stream.get("framerate", config.framerate))
    try:
        report = measure_frame_rate(config)
    except FrameRateError as exc:
        return [issue(stream, "video_frame_rate_match", f"Frame-rate measurement failed: {exc}")]
    delta = report.measured_fps - expected
    if abs(delta) <= FRAME_RATE_TOLERANCE_FPS:
        return []
    return [
        issue(
            stream,
            "video_frame_rate_match",
            f"Measured frame rate {report.measured_fps:.2f} fps differs from configured {expected:.2f} fps by {delta:+.2f} fps",
        )
    ]


def ts_sample(config: VideoFeedConfig, sample_seconds: float) -> tuple[bytes, float | None]:
    if config.protocol == "dash":
        paths = sorted(Path(config.dash_dir).glob("*.ts"), key=lambda path: path.stat().st_mtime)[-15:]
        return b"".join(path.read_bytes() for path in paths), len(paths) * DASH_SEGMENT_DURATION_SECONDS if paths else None
    return srt_ts_sample(config, sample_seconds), sample_seconds


def srt_ts_sample(config: VideoFeedConfig, sample_seconds: float) -> bytes:
    args = ["gst-launch-1.0", "-q", "srtsrc", f"uri={config.endpoint}", "!", "fdsink", "fd=1"]
    try:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError:
        return b""
    try:
        stdout, _ = process.communicate(timeout=sample_seconds)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            stdout, _ = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, _ = process.communicate(timeout=3)
    return stdout


def issue(stream: dict, monitor_id: str, message: str) -> MonitorIssue:
    spec = SPEC_BY_ID[monitor_id]
    return MonitorIssue(
        stream_id=stream["id"],
        stream_name=stream["name"],
        monitor_id=spec.id,
        monitor_name=spec.name,
        severity=spec.severity,
        message=message,
    )


def apply_issues(state: dict, issues: list[MonitorIssue], now: float, repeat_seconds: float, history_limit: int) -> dict:
    state.setdefault("alarms", [])
    state.setdefault("events", [])
    alarms = {alarm["id"]: alarm for alarm in state["alarms"]}
    active_ids = {alarm["id"] for alarm in state["alarms"] if alarm.get("active")}
    issue_ids = {item.alarm_id for item in issues}

    for item in issues:
        alarm = alarms.get(item.alarm_id)
        if not alarm or not alarm.get("active"):
            alarm = alarm_payload(item, now)
            alarms[item.alarm_id] = alarm
            append_event(state, "alarm_raised", alarm, now)
        elif now - float(alarm.get("lastEventAt", 0)) >= repeat_seconds:
            alarm.update({"lastEventAt": now, "message": item.message})
            append_event(state, "alarm_active", alarm, now)

    for alarm_id in sorted(active_ids - issue_ids):
        alarm = alarms[alarm_id]
        alarm.update({"active": False, "status": "steady", "clearedAt": iso(now), "lastEventAt": now})
        append_event(state, "alarm_cleared", alarm, now)

    state["alarms"] = sorted(alarms.values(), key=lambda alarm: (not alarm.get("active"), alarm["streamName"], alarm["monitorName"]))
    state["events"] = state["events"][-history_limit:]
    state["updatedAt"] = iso(now)
    state["monitors"] = monitor_catalog_payload()
    return state


def apply_alert_profiles(state: dict, streams: list[dict], issues: list[MonitorIssue], now: float) -> list[MonitorIssue]:
    streams_by_id = {stream["id"]: stream for stream in streams}
    pending = {item["id"]: item for item in state.get("pending", [])}
    active_ids = {alarm["id"] for alarm in state.get("alarms", []) if alarm.get("active")}
    filtered = []
    next_pending = {}
    for item in issues:
        stream = streams_by_id.get(item.stream_id, {})
        profile = alert_profile_from_stream(stream)
        enabled = profile["enabledMonitorIds"]
        if enabled is not None and item.monitor_id not in enabled:
            continue
        delay = profile["delaySeconds"]
        first_seen = float(pending.get(item.alarm_id, {}).get("firstSeenAt", now))
        if item.alarm_id in active_ids or delay <= 0 or now - first_seen >= delay:
            filtered.append(item)
        else:
            next_pending[item.alarm_id] = {
                "id": item.alarm_id,
                "streamId": item.stream_id,
                "monitorId": item.monitor_id,
                "firstSeenAt": first_seen,
            }
    state["pending"] = sorted(next_pending.values(), key=lambda item: item["id"])
    return filtered


def alarm_payload(item: MonitorIssue, now: float) -> dict:
    return {
        "id": item.alarm_id,
        "streamId": item.stream_id,
        "streamName": item.stream_name,
        "monitorId": item.monitor_id,
        "monitorName": item.monitor_name,
        "severity": item.severity,
        "active": True,
        "status": "active",
        "raisedAt": iso(now),
        "clearedAt": "",
        "lastEventAt": now,
        "message": item.message,
    }


def append_event(state: dict, event_type: str, alarm: dict, now: float):
    state["events"].append(
        {
            "id": f"{int(now * 1000)}-{len(state['events']) + 1}",
            "time": iso(now),
            "type": event_type,
            "alarmId": alarm["id"],
            "streamId": alarm["streamId"],
            "streamName": alarm["streamName"],
            "monitorId": alarm["monitorId"],
            "monitorName": alarm["monitorName"],
            "severity": alarm["severity"],
            "message": alarm["message"],
        }
    )


def iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def run_monitor_once(
    gui_state: dict,
    state: dict,
    now: float,
    repeat_seconds: float,
    history_limit: int,
    srt_host: str,
    validator: Callable[[VideoFeedConfig], ValidationReport] = validate_config,
    tr101_checker: Callable[[dict, VideoFeedConfig], list[MonitorIssue]] = tr101_issues_for_stream,
    loudness_checker: Callable[[dict, VideoFeedConfig], list[MonitorIssue]] = loudness_issues_for_stream,
    frame_rate_checker: Callable[[dict, VideoFeedConfig], list[MonitorIssue]] = frame_rate_issues_for_stream,
) -> dict:
    issues = []
    for stream in gui_state.get("streams", []):
        if stream.get("status") != "running":
            continue
        config = None
        try:
            config = config_for_stream(stream, srt_host)
            report = validator(config)
        except Exception as exc:  # ponytail: monitor stays alive; classify probe crashes as feed reachability alarms.
            report = ValidationReport(endpoint=stream.get("endpoint", ""), errors=[str(exc)])
        issues.extend(issues_for_report(stream, report))
        if config is None:
            continue
        try:
            issues.extend(tr101_checker(stream, config))
        except Exception:
            pass
        if report.video_present:
            try:
                issues.extend(frame_rate_checker(stream, config))
            except Exception:
                pass
        if report.audio_present:
            try:
                issues.extend(loudness_checker(stream, config))
            except Exception:
                pass
    issues = apply_alert_profiles(state, gui_state.get("streams", []), issues, now)
    return apply_issues(state, issues, now, repeat_seconds, history_limit)


def run_monitor(
    gui_state_url: str,
    state_path: str,
    poll_seconds: float,
    repeat_seconds: float,
    history_limit: int,
    srt_host: str,
    once: bool = False,
) -> int:
    while True:
        state = load_monitor_state(state_path)
        try:
            gui_state = fetch_gui_state(gui_state_url)
            state = run_monitor_once(gui_state, state, time.time(), repeat_seconds, history_limit, srt_host)
        except Exception as exc:  # ponytail: no supervisor yet; emit the monitor's own health into the same event file.
            state = apply_issues(
                state,
                [
                    MonitorIssue(
                        stream_id="monitor",
                        stream_name="Monitor",
                        monitor_id="feed_reachable",
                        monitor_name="Feed reachable",
                        severity="critical",
                        message=f"Monitor poll failed: {exc}",
                    )
                ],
                time.time(),
                repeat_seconds,
                history_limit,
            )
        write_monitor_state(state_path, state)
        if once:
            return 0
        time.sleep(poll_seconds)
