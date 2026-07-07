from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse
from urllib.request import urlopen

from .feed import VideoFeedConfig
from .gui import controls_for_mode, profile_for
from .profile import load_profile
from .validator import ValidationReport, validate_config


DEFAULT_MONITOR_STATE_PATH = "/tmp/videosim-monitor/state.json"


@dataclass(frozen=True)
class MonitorSpec:
    id: str
    name: str
    priority: str
    severity: str
    implemented: bool
    description: str


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


MONITOR_SPECS = [
    MonitorSpec("feed_reachable", "Feed reachable", "platform", "critical", True, "Input feed can be ingested."),
    MonitorSpec("essence_video_present", "Video present", "platform", "critical", True, "Expected video essence is present."),
    MonitorSpec("essence_audio_present", "Audio present", "platform", "critical", True, "Expected audio essence is present."),
    MonitorSpec("essence_captions_present", "Captions present", "platform", "major", True, "Expected captions are present."),
    MonitorSpec("black_video_detected", "Black video detected", "platform", "major", True, "Expected black-video profile validates."),
    MonitorSpec("frozen_video_detected", "Frozen video detected", "platform", "major", True, "Expected frozen-video profile validates."),
    MonitorSpec("tr101_1_1_ts_sync_loss", "TS sync loss", "TR101 priority 1", "critical", False, "Loss of MPEG-2 TS synchronization."),
    MonitorSpec("tr101_1_2_sync_byte_error", "Sync byte error", "TR101 priority 1", "critical", False, "Sync byte not equal to 0x47."),
    MonitorSpec("tr101_1_3_pat_error", "PAT error", "TR101 priority 1", "critical", False, "PAT missing, wrong table id, or scrambled."),
    MonitorSpec("tr101_1_3a_pat_error_2", "PAT error 2", "TR101 priority 1", "critical", False, "Updated PAT repetition/table-id check."),
    MonitorSpec("tr101_1_4_continuity_count_error", "Continuity count error", "TR101 priority 1", "critical", False, "Incorrect order, repeat, or lost packet."),
    MonitorSpec("tr101_1_5_pmt_error", "PMT error", "TR101 priority 1", "critical", False, "PMT missing or scrambled."),
    MonitorSpec("tr101_1_5a_pmt_error_2", "PMT error 2", "TR101 priority 1", "critical", False, "Updated PMT repetition/table-id check."),
    MonitorSpec("tr101_1_6_pid_error", "PID error", "TR101 priority 1", "critical", False, "Referenced PID missing for configured period."),
    MonitorSpec("tr101_2_1_transport_error", "Transport error", "TR101 priority 2", "major", False, "Transport error indicator set."),
    MonitorSpec("tr101_2_2_crc_error", "CRC error", "TR101 priority 2", "major", False, "PSI/SI table CRC error."),
    MonitorSpec("tr101_2_3_pcr_error", "PCR error", "TR101 priority 2", "major", False, "PCR discontinuity or repetition fault."),
    MonitorSpec("tr101_2_3a_pcr_repetition_error", "PCR repetition error", "TR101 priority 2", "major", False, "PCR interval greater than 100 ms."),
    MonitorSpec("tr101_2_3b_pcr_discontinuity_indicator_error", "PCR discontinuity indicator error", "TR101 priority 2", "major", False, "PCR jump without discontinuity indicator."),
    MonitorSpec("tr101_2_4_pcr_accuracy_error", "PCR accuracy error", "TR101 priority 2", "major", False, "PCR accuracy outside +/-500 ns."),
    MonitorSpec("tr101_2_5_pts_error", "PTS error", "TR101 priority 2", "major", False, "PTS repetition period greater than 700 ms."),
    MonitorSpec("tr101_2_6_cat_error", "CAT error", "TR101 priority 2", "major", False, "CAT missing or malformed for scrambled packets."),
]

SPEC_BY_ID = {spec.id: spec for spec in MONITOR_SPECS}


def empty_monitor_state() -> dict:
    return {"updatedAt": "", "alarms": [], "events": [], "monitors": [asdict(spec) for spec in MONITOR_SPECS]}


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
    payload["monitors"] = [asdict(spec) for spec in MONITOR_SPECS]
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
    if stream["protocol"] == "dash":
        return VideoFeedConfig(
            **{
                **config.__dict__,
                "dash_dir": f"/tmp/videosim-dash/{stream['id']}",
                "dash_base_url": stream["endpoint"].rsplit("/", 1)[0],
            }
        )
    parsed = urlparse(stream["endpoint"])
    return VideoFeedConfig(**{**config.__dict__, "srt_host": srt_host, "port": parsed.port or config.port})


def issues_for_report(stream: dict, report: ValidationReport) -> list[MonitorIssue]:
    controls = controls_for_mode(stream["mode"])
    issues = []
    if not report.reachable:
        issues.append(issue(stream, "feed_reachable", "Feed is unreachable or expected streams are missing"))
    if controls["video"] and not report.video_present:
        issues.append(issue(stream, "essence_video_present", "Expected video is absent"))
    if controls["audio"] and not report.audio_present:
        issues.append(issue(stream, "essence_audio_present", "Expected audio is absent"))
    if controls["captions"] and not report.captions_present:
        issues.append(issue(stream, "essence_captions_present", "Expected captions are absent"))
    if controls["black_video"] and not report.black_video:
        issues.append(issue(stream, "black_video_detected", "Black-video profile did not validate"))
    if controls["frozen_video"] and not report.frozen_video:
        issues.append(issue(stream, "frozen_video_detected", "Frozen-video profile did not validate"))
    return issues


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
    state["monitors"] = [asdict(spec) for spec in MONITOR_SPECS]
    return state


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
) -> dict:
    issues = []
    for stream in gui_state.get("streams", []):
        if stream.get("status") != "running":
            continue
        try:
            report = validator(config_for_stream(stream, srt_host))
        except Exception as exc:  # ponytail: monitor stays alive; classify probe crashes as feed reachability alarms.
            report = ValidationReport(endpoint=stream.get("endpoint", ""), errors=[str(exc)])
        issues.extend(issues_for_report(stream, report))
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
