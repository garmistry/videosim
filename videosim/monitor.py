from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
import subprocess
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse
from urllib.request import urlopen

from .alert_profile import alert_profile_from_stream, alert_profile_payload
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
from .probe_deadline import probe_timeout, use_probe_deadline
from .tr101 import TR101_INDICATORS, analyze_ts
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


class MonitorIssues(list[MonitorIssue]):
    """Issues plus the monitor IDs conclusively evaluated by a checker.

    The list-compatible surface preserves the trusted-lab monitor API while
    allowing worker v2 to distinguish a healthy observation from an omitted or
    inconclusive check.
    """

    def __init__(
        self,
        items=(),
        *,
        evaluated_monitor_ids=(),
        statuses: dict[str, str] | None = None,
    ):
        super().__init__(items)
        self.evaluated_monitor_ids = tuple(evaluated_monitor_ids)
        self.statuses = dict(statuses or {})


def empty_monitor_state() -> dict:
    return {
        "updatedAt": "",
        "alarms": [],
        "events": [],
        "pending": [],
        "monitorObservations": [],
        "monitors": monitor_catalog_payload(),
    }


def monitor_state_for_stream_ids(state: dict, stream_ids: set[str]) -> dict:
    """Return monitor state scoped to the worker's current assignment."""

    scoped = dict(state)
    collections = ["alarms", "events", "pending"]
    if "monitorObservations" in state:
        collections.append("monitorObservations")
    for collection in collections:
        scoped[collection] = [
            dict(item)
            for item in state.get(collection, [])
            if isinstance(item, dict) and item.get("streamId") in stream_ids
        ]
    metrics = state.get("probeMetrics")
    if isinstance(metrics, dict):
        metrics = dict(metrics)
        per_stream = metrics.get("streams")
        if isinstance(per_stream, list):
            filtered = [
                dict(item)
                for item in per_stream
                if isinstance(item, dict) and item.get("streamId") in stream_ids
            ]
            outcomes: dict[str, int] = {}
            for item in filtered:
                outcome = str(item.get("outcome", "unknown"))
                outcomes[outcome] = outcomes.get(outcome, 0) + 1
            metrics["streams"] = filtered
            metrics["streamCount"] = len({item["streamId"] for item in filtered})
            metrics["checkCount"] = len(filtered)
            metrics["outcomes"] = outcomes
        scoped["probeMetrics"] = metrics
    return scoped


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
    payload.setdefault("monitorObservations", [])
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
    config = load_profile(profile_for(stream["protocol"], "normal" if stream.get("source") == "external" else stream["mode"]))
    overrides = {
        key: stream[key]
        for key in ("width", "height", "framerate")
        if key in stream and stream[key] not in (None, "")
    }
    if stream.get("source") == "external":
        return replace(config, **overrides, protocol=stream["protocol"], external_endpoint=stream["endpoint"], passive=True)
    if stream.get("monitorEndpoint"):
        return replace(config, **overrides, protocol=stream["protocol"], external_endpoint=stream["monitorEndpoint"])
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


def validation_monitor_ids(stream: dict) -> tuple[str, ...]:
    controls = (
        {field: False for field in ("video", "audio", "captions", "black_video", "frozen_video")}
        if stream.get("source") == "external"
        else controls_for_mode(stream["mode"])
    )
    enabled = monitor_alert_profile(stream)["enabledMonitorIds"]
    explicit = set(enabled or []) if enabled is not None else set()

    def expected(control: str, monitor_id: str) -> bool:
        return (
            (enabled is None and stream.get("source") == "external")
            or controls[control]
            or monitor_id in explicit
        )

    monitor_ids = ["feed_reachable"]
    for control, monitor_id in (
        ("video", "essence_video_present"),
        ("audio", "essence_audio_present"),
        ("captions", "essence_captions_present"),
        ("black_video", "black_video_detected"),
        ("frozen_video", "frozen_video_detected"),
    ):
        if expected(control, monitor_id):
            monitor_ids.append(monitor_id)
    return tuple(monitor_ids)


def issues_for_report(stream: dict, report: ValidationReport) -> list[MonitorIssue]:
    failures = {
        "feed_reachable": (not report.reachable, "Feed is unreachable or expected streams are missing"),
        "essence_video_present": (not report.video_present, "Expected video is absent"),
        "essence_audio_present": (not report.audio_present, "Expected audio is absent"),
        "essence_captions_present": (not report.captions_present, "Expected captions are absent"),
        "black_video_detected": (not report.black_video, "Black-video profile did not validate"),
        "frozen_video_detected": (not report.frozen_video, "Frozen-video profile did not validate"),
    }
    return [
        issue(stream, monitor_id, failures[monitor_id][1])
        for monitor_id in validation_monitor_ids(stream)
        if failures[monitor_id][0]
    ]


def tr101_issues_for_stream(stream: dict, config: VideoFeedConfig, sample_seconds: float = 1.0) -> list[MonitorIssue]:
    data, duration = ts_sample(config, sample_seconds)
    if not data:
        return MonitorIssues()
    report = analyze_ts(data, duration)
    return MonitorIssues(
        [
            issue(stream, indicator, report.messages.get(indicator, SPEC_BY_ID[indicator].description))
            for indicator, active in report.indicators.items()
            if active
        ],
        evaluated_monitor_ids=TR101_INDICATORS,
    )


def loudness_issues_for_stream(stream: dict, config: VideoFeedConfig, sample_seconds: float = 5.0) -> list[MonitorIssue]:
    monitor_ids = (
        "loudness_bs1770_measurement",
        "loudness_ebu_r128_integrated",
        "loudness_ebu_r128_true_peak",
        "loudness_atsc_a85_integrated",
    )
    if not controls_for_mode(stream["mode"])["audio"]:
        return MonitorIssues()
    try:
        report = measure_loudness(config, sample_seconds)
    except LoudnessError as exc:
        return MonitorIssues(
            [issue(stream, "loudness_bs1770_measurement", f"BS.1770 loudness measurement failed: {exc}")],
            statuses={"loudness_bs1770_measurement": "error"},
        )

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
    return MonitorIssues(issues, evaluated_monitor_ids=monitor_ids)


def frame_rate_issues_for_stream(stream: dict, config: VideoFeedConfig) -> list[MonitorIssue]:
    if not controls_for_mode(stream["mode"])["video"]:
        return MonitorIssues()
    expected = frame_rate_float(stream.get("framerate", config.framerate))
    try:
        report = measure_frame_rate(config)
    except FrameRateError as exc:
        return MonitorIssues(
            [issue(stream, "video_frame_rate_match", f"Frame-rate measurement failed: {exc}")],
            statuses={"video_frame_rate_match": "error"},
        )
    delta = report.measured_fps - expected
    if abs(delta) <= FRAME_RATE_TOLERANCE_FPS:
        return MonitorIssues(evaluated_monitor_ids=("video_frame_rate_match",))
    return MonitorIssues(
        [
            issue(
                stream,
                "video_frame_rate_match",
                f"Measured frame rate {report.measured_fps:.2f} fps differs from configured {expected:.2f} fps by {delta:+.2f} fps",
            )
        ],
        evaluated_monitor_ids=("video_frame_rate_match",),
    )


def ts_sample(config: VideoFeedConfig, sample_seconds: float) -> tuple[bytes, float | None]:
    if config.protocol == "dash":
        paths = sorted(Path(config.dash_dir).glob("*.ts"), key=lambda path: path.stat().st_mtime)[-15:]
        chunks = []
        for path in paths:
            probe_timeout(float("inf"))
            chunks.append(path.read_bytes())
            probe_timeout(float("inf"))
        return b"".join(chunks), len(paths) * DASH_SEGMENT_DURATION_SECONDS if paths else None
    return srt_ts_sample(config, sample_seconds), sample_seconds


def srt_ts_sample(config: VideoFeedConfig, sample_seconds: float) -> bytes:
    args = ["gst-launch-1.0", "-q", "srtsrc", f"uri={config.endpoint}", "!", "fdsink", "fd=1"]
    timeout = probe_timeout(sample_seconds)
    budget_limited = timeout < sample_seconds
    try:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError:
        return b""
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.terminate()
        try:
            cleanup_timeout = probe_timeout(3)
            stdout, _ = process.communicate(timeout=cleanup_timeout)
        except TimeoutError:
            process.kill()
            process.communicate(timeout=1)
            raise
        except subprocess.TimeoutExpired as cleanup_exc:
            process.kill()
            stdout, _ = process.communicate(timeout=1)
            if cleanup_timeout < 3:
                raise TimeoutError("stream probe budget exhausted") from cleanup_exc
        if budget_limited:
            raise TimeoutError("stream probe budget exhausted") from exc
        probe_timeout(float("inf"))
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


def apply_issues(
    state: dict,
    issues: list[MonitorIssue],
    now: float,
    repeat_seconds: float,
    history_limit: int,
    clearable_alarm_ids: set[str] | None = None,
) -> dict:
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

    clearable_alarm_ids = active_ids if clearable_alarm_ids is None else clearable_alarm_ids
    for alarm_id in sorted((active_ids - issue_ids) & clearable_alarm_ids):
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
        profile = monitor_alert_profile(stream)
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


def monitor_alert_profile(stream: dict) -> dict:
    if stream.get("source") == "external" and "alertProfile" not in stream:
        return alert_profile_payload([], 0)
    return alert_profile_from_stream(stream)


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


def _merge_monitor_results(
    state: dict,
    streams: list[dict],
    results: list[dict],
    now: float,
    history_limit: int,
    started: float,
    monotonic: Callable[[], float],
    *,
    preserve_observations: bool = False,
    preserve_metrics: bool = False,
) -> dict:
    stream_ids = {stream["id"] for stream in streams}
    combined = dict(state)
    for collection in ("alarms", "pending"):
        retained = [
            item
            for item in state.get(collection, [])
            if item.get("streamId") not in stream_ids
        ]
        combined[collection] = retained + [
            item for result in results for item in result.get(collection, [])
        ]
    retained_observations = list(state.get("monitorObservations", [])) if preserve_observations else [
        item
        for item in state.get("monitorObservations", [])
        if item.get("streamId") not in stream_ids
    ]
    combined["monitorObservations"] = retained_observations + [
        item
        for result in results
        for item in result.get("monitorObservations", [])
    ]
    combined["alarms"] = sorted(
        combined["alarms"],
        key=lambda alarm: (
            not alarm.get("active"),
            alarm.get("streamName", ""),
            alarm.get("monitorName", ""),
        ),
    )
    combined["pending"] = sorted(
        combined["pending"], key=lambda item: item.get("id", "")
    )
    combined["events"] = (
        [
            item
            for item in state.get("events", [])
            if item.get("streamId") not in stream_ids
        ]
        + [item for result in results for item in result.get("events", [])]
    )[-history_limit:]

    old_metrics = state.get("probeMetrics", {})
    old_stream_metrics = (
        old_metrics.get("streams", []) if isinstance(old_metrics, dict) else []
    )
    retained_metrics = old_stream_metrics if preserve_metrics else [
        item for item in old_stream_metrics if item.get("streamId") not in stream_ids
    ]
    stream_metrics = retained_metrics + [
        item
        for result in results
        for item in result.get("probeMetrics", {}).get("streams", [])
    ]
    outcomes: dict[str, int] = {}
    for item in stream_metrics:
        outcome = item.get("outcome", "unknown")
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    combined["updatedAt"] = iso(now)
    combined["monitors"] = monitor_catalog_payload()
    combined["probeMetrics"] = {
        "observedAt": iso(now),
        "batchDurationMs": round(max(0.0, (monotonic() - started) * 1000), 3),
        "streamCount": len({item.get("streamId") for item in stream_metrics}),
        "checkCount": len(stream_metrics),
        "outcomes": outcomes,
        "streams": stream_metrics,
    }
    return combined


def _run_monitor_concurrent(
    gui_state: dict,
    state: dict,
    now: float,
    repeat_seconds: float,
    history_limit: int,
    srt_host: str,
    max_concurrency: int,
    stream_budget_seconds: float,
    validator: Callable[[VideoFeedConfig], ValidationReport],
    tr101_checker: Callable[[dict, VideoFeedConfig], list[MonitorIssue]],
    loudness_checker: Callable[[dict, VideoFeedConfig], list[MonitorIssue]],
    frame_rate_checker: Callable[[dict, VideoFeedConfig], list[MonitorIssue]],
    monotonic: Callable[[], float],
) -> dict:
    streams = [
        stream
        for stream in gui_state.get("streams", [])
        if stream.get("status") == "running"
    ]
    started = monotonic()

    def monitor_stream(
        stream: dict,
        phase_state: dict,
        *,
        validation_only: bool = False,
        probe_contexts: dict | None = None,
    ) -> dict:
        return run_monitor_once(
            {"streams": [stream]},
            monitor_state_for_stream_ids(phase_state, {stream["id"]}),
            now,
            repeat_seconds,
            history_limit,
            srt_host,
            validator=validator,
            tr101_checker=tr101_checker,
            loudness_checker=loudness_checker,
            frame_rate_checker=frame_rate_checker,
            monotonic=monotonic,
            stream_budget_seconds=stream_budget_seconds,
            _validation_only=validation_only,
            _probe_contexts=probe_contexts,
        )

    # ponytail: two bounded phases protect core validation freshness; add more cost
    # tiers only when production measurements justify them.
    with ThreadPoolExecutor(max_workers=min(max_concurrency, len(streams))) as pool:
        validation_results = list(
            pool.map(lambda stream: monitor_stream(stream, state, validation_only=True), streams)
        )
        validation_state = _merge_monitor_results(
            state,
            streams,
            validation_results,
            now,
            history_limit,
            started,
            monotonic,
        )
        probe_contexts = {
            stream_id: context
            for result in validation_results
            for stream_id, context in result.get("_probeContexts", {}).items()
        }
        deep_results = list(
            pool.map(
                lambda stream: monitor_stream(
                    stream,
                    validation_state,
                    probe_contexts=probe_contexts,
                ),
                streams,
            )
        )

    return _merge_monitor_results(
        validation_state,
        streams,
        deep_results,
        now,
        history_limit,
        started,
        monotonic,
        preserve_observations=True,
        preserve_metrics=True,
    )


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
    monotonic: Callable[[], float] = time.monotonic,
    *,
    max_concurrency: int = 1,
    stream_budget_seconds: float = 0,
    _validation_only: bool = False,
    _probe_contexts: dict | None = None,
) -> dict:
    if max_concurrency < 1:
        raise ValueError("max_concurrency must be at least 1")
    if stream_budget_seconds < 0:
        raise ValueError("stream_budget_seconds must be zero or greater")
    if max_concurrency > 1 and any(
        stream.get("status") == "running" for stream in gui_state.get("streams", [])
    ):
        return _run_monitor_concurrent(
            gui_state,
            state,
            now,
            repeat_seconds,
            history_limit,
            srt_host,
            max_concurrency,
            stream_budget_seconds,
            validator,
            tr101_checker,
            loudness_checker,
            frame_rate_checker,
            monotonic,
        )
    issues = []
    probe_metrics = []
    monitor_observations: dict[tuple[str, str], dict] = {}
    next_probe_contexts = {}
    batch_started = monotonic()

    def record(stream: dict, check: str, outcome: str, started: float | None = None, detail: str = ""):
        duration_ms = 0.0 if started is None else max(0.0, (monotonic() - started) * 1000)
        item = {
            "streamId": stream["id"],
            "protocol": stream.get("protocol", "unknown"),
            "source": stream.get("source", "generated"),
            "check": check,
            "outcome": outcome,
            "durationMs": round(duration_ms, 3),
        }
        if detail:
            item["detail"] = detail[:200]
        probe_metrics.append(item)

    def observe(stream: dict, monitor_id: str, status: str, message: str = ""):
        if monitor_id not in SPEC_BY_ID:
            return
        key = (stream["id"], monitor_id)
        current = monitor_observations.get(key)
        priority = {
            "unhealthy": 5,
            "error": 4,
            "timeout": 4,
            "unknown": 3,
            "skipped": 2,
            "healthy": 1,
        }
        if current is not None and priority.get(current["status"], 0) > priority.get(status, 0):
            return
        monitor_observations[key] = {
            "streamId": stream["id"],
            "monitorId": monitor_id,
            "status": status,
            "message": (message or SPEC_BY_ID[monitor_id].description)[:200],
        }

    def observe_checker(stream: dict, checker_issues):
        issue_by_id = {item.monitor_id: item for item in checker_issues}
        statuses = getattr(checker_issues, "statuses", {})
        evaluated = getattr(checker_issues, "evaluated_monitor_ids", ())
        for monitor_id in evaluated:
            item = issue_by_id.get(monitor_id)
            observe(stream, monitor_id, "unhealthy" if item else "healthy", item.message if item else "")
        for monitor_id, status in statuses.items():
            item = issue_by_id.get(monitor_id)
            observe(stream, monitor_id, status, item.message if item else "")
        for monitor_id, item in issue_by_id.items():
            if monitor_id not in statuses:
                observe(stream, monitor_id, "unhealthy", item.message)

    for stream in gui_state.get("streams", []):
        if stream.get("status") != "running":
            continue
        probe_context = (_probe_contexts or {}).get(stream["id"])
        if probe_context is None:
            config = None
            report = None
            stream_deadline = (
                monotonic() + stream_budget_seconds
                if stream_budget_seconds
                else None
            )
            media_deadline = (
                time.monotonic() + stream_budget_seconds
                if stream_budget_seconds
                else None
            )
        else:
            config, report, stream_deadline, media_deadline = probe_context

        def check_budget():
            if stream_deadline is not None and monotonic() >= stream_deadline:
                raise TimeoutError("stream probe budget exhausted")

        validation_issues = []
        if probe_context is None:
            validation_started = monotonic()
            try:
                config = config_for_stream(stream, srt_host)
                with use_probe_deadline(media_deadline):
                    report = validator(config)
                check_budget()
                validation_issues = issues_for_report(stream, report)
                issues_by_id = {item.monitor_id: item for item in validation_issues}
                for monitor_id in validation_monitor_ids(stream):
                    item = issues_by_id.get(monitor_id)
                    observe(stream, monitor_id, "unhealthy" if item else "healthy", item.message if item else "")
                record(stream, "validation", "issue" if validation_issues else "success", validation_started)
            except Exception as exc:  # ponytail: keep the batch alive; probe crashes stay inconclusive instead of becoming feed alarms.
                report = ValidationReport(endpoint=stream.get("endpoint", ""), errors=[str(exc)])
                outcome = "timeout" if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)) else "error"
                for monitor_id in validation_monitor_ids(stream):
                    observe(stream, monitor_id, outcome, str(exc))
                record(stream, "validation", outcome, validation_started, str(exc))
        issues.extend(validation_issues)
        if _validation_only:
            next_probe_contexts[stream["id"]] = (
                config,
                report,
                stream_deadline,
                media_deadline,
            )
            continue
        if config is None:
            record(stream, "tr101", "skipped", detail="validation configuration unavailable")
            record(stream, "frame_rate", "skipped", detail="validation configuration unavailable")
            record(stream, "loudness", "skipped", detail="validation configuration unavailable")
            continue

        tr101_started = monotonic()
        try:
            check_budget()
            with use_probe_deadline(media_deadline):
                tr101_issues = tr101_checker(stream, config)
            check_budget()
            issues.extend(tr101_issues)
            observe_checker(stream, tr101_issues)
            record(stream, "tr101", "issue" if tr101_issues else "success", tr101_started)
        except Exception as exc:
            outcome = "timeout" if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)) else "error"
            for monitor_id in TR101_INDICATORS:
                observe(stream, monitor_id, outcome, str(exc))
            record(stream, "tr101", outcome, tr101_started, str(exc))

        if stream_deadline is not None and monotonic() >= stream_deadline:
            observe(stream, "video_frame_rate_match", "timeout", "stream probe budget exhausted")
            record(stream, "frame_rate", "timeout", detail="stream probe budget exhausted")
        elif stream.get("source") == "external":
            observe(stream, "video_frame_rate_match", "skipped", "external feed has no configured frame-rate expectation")
            record(stream, "frame_rate", "skipped", detail="external feed has no configured frame-rate expectation")
        elif not report.video_present:
            observe(stream, "video_frame_rate_match", "skipped", "video is not present")
            record(stream, "frame_rate", "skipped", detail="video is not present")
        else:
            frame_started = monotonic()
            try:
                check_budget()
                with use_probe_deadline(media_deadline):
                    frame_issues = frame_rate_checker(stream, config)
                check_budget()
                issues.extend(frame_issues)
                observe_checker(stream, frame_issues)
                record(stream, "frame_rate", "issue" if frame_issues else "success", frame_started)
            except Exception as exc:
                outcome = "timeout" if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)) else "error"
                observe(stream, "video_frame_rate_match", outcome, str(exc))
                record(stream, "frame_rate", outcome, frame_started, str(exc))

        loudness_monitor_ids = (
            "loudness_bs1770_measurement",
            "loudness_ebu_r128_integrated",
            "loudness_ebu_r128_true_peak",
            "loudness_atsc_a85_integrated",
        )
        if stream_deadline is not None and monotonic() >= stream_deadline:
            for monitor_id in loudness_monitor_ids:
                observe(stream, monitor_id, "timeout", "stream probe budget exhausted")
            record(stream, "loudness", "timeout", detail="stream probe budget exhausted")
        elif stream.get("source") == "external":
            for monitor_id in loudness_monitor_ids:
                observe(stream, monitor_id, "skipped", "external feed has no configured loudness expectation")
            record(stream, "loudness", "skipped", detail="external feed has no configured loudness expectation")
        elif not report.audio_present:
            for monitor_id in loudness_monitor_ids:
                observe(stream, monitor_id, "skipped", "audio is not present")
            record(stream, "loudness", "skipped", detail="audio is not present")
        else:
            loudness_started = monotonic()
            try:
                check_budget()
                with use_probe_deadline(media_deadline):
                    loudness_issues = loudness_checker(stream, config)
                check_budget()
                issues.extend(loudness_issues)
                observe_checker(stream, loudness_issues)
                record(stream, "loudness", "issue" if loudness_issues else "success", loudness_started)
            except Exception as exc:
                outcome = "timeout" if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)) else "error"
                for monitor_id in loudness_monitor_ids:
                    observe(stream, monitor_id, outcome, str(exc))
                record(stream, "loudness", outcome, loudness_started, str(exc))

    clearable_alarm_ids = {
        f"{stream_id}:{monitor_id}"
        for (stream_id, monitor_id), observation in monitor_observations.items()
        if observation["status"] in {"healthy", "unhealthy"}
    }
    preserved_pending = {
        item["id"]: item
        for item in state.get("pending", [])
        if item.get("id") not in clearable_alarm_ids
    }
    issues = apply_alert_profiles(state, gui_state.get("streams", []), issues, now)
    state["pending"] = sorted(
        {**preserved_pending, **{item["id"]: item for item in state["pending"]}}.values(),
        key=lambda item: item["id"],
    )
    result = apply_issues(
        state,
        issues,
        now,
        repeat_seconds,
        history_limit,
        clearable_alarm_ids,
    )
    outcomes: dict[str, int] = {}
    for item in probe_metrics:
        outcomes[item["outcome"]] = outcomes.get(item["outcome"], 0) + 1
    result["monitorObservations"] = [
        monitor_observations[key]
        for key in sorted(monitor_observations)
    ]
    result["probeMetrics"] = {
        "observedAt": iso(now),
        "batchDurationMs": round(max(0.0, (monotonic() - batch_started) * 1000), 3),
        "streamCount": len({item["streamId"] for item in probe_metrics}),
        "checkCount": len(probe_metrics),
        "outcomes": outcomes,
        "streams": probe_metrics,
    }
    if _validation_only:
        result["_probeContexts"] = next_probe_contexts
    return result


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
