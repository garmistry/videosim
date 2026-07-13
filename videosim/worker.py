from __future__ import annotations

import json
import math
import random
import resource
import signal
import socket
import ssl
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .monitor import (
    BATCH_BUDGET_EXHAUSTED,
    empty_monitor_state,
    monitor_state_for_stream_ids,
    run_monitor_once,
)
from .report_spool import EncryptedReportSpool, ReportSpoolError


MAX_ASSIGNMENT_CONFLICT_REFETCHES = 3
DEFAULT_RETRY_ATTEMPTS = 5
DEFAULT_RETRY_BASE_SECONDS = 0.25
WORKER_API_VERSION_V2 = "videosim.worker/v2"
CONTRACT_FIELDS = (
    "apiVersion",
    "controlPlaneInstanceId",
    "assignmentGeneration",
    "assignmentToken",
)
T = TypeVar("T")


def default_worker_id() -> str:
    return socket.gethostname()


def build_ssl_context(ca_file: str = "", cert_file: str = "", key_file: str = "") -> ssl.SSLContext | None:
    if not any((ca_file, cert_file, key_file)):
        return None
    if not ca_file:
        raise ValueError("worker TLS CA file is required when TLS options are configured")
    if bool(cert_file) != bool(key_file):
        raise ValueError("worker TLS certificate and key must be configured together")
    for label, path in (("CA", ca_file), ("certificate", cert_file), ("key", key_file)):
        if path and not Path(path).is_file():
            raise ValueError(f"worker TLS {label} file does not exist: {path}")
    context = ssl.create_default_context(cafile=ca_file)
    if cert_file:
        context.load_cert_chain(certfile=cert_file, keyfile=key_file)
    return context


def _open(request, ssl_context: ssl.SSLContext | None):
    options = {"timeout": 10}
    if ssl_context is not None:
        options["context"] = ssl_context
    return urlopen(request, **options)


def fetch_assignments(
    control_plane_url: str,
    worker_id: str,
    ssl_context: ssl.SSLContext | None = None,
    worker_incarnation_id: str = "",
) -> dict:
    parameters = {"worker_id": worker_id}
    if worker_incarnation_id:
        parameters["worker_incarnation_id"] = worker_incarnation_id
    query = urlencode(parameters)
    with _open(f"{control_plane_url.rstrip('/')}/api/workers/assignments?{query}", ssl_context) as response:
        return json.loads(response.read().decode("utf-8"))


def post_heartbeat(
    control_plane_url: str,
    worker_id: str,
    ssl_context: ssl.SSLContext | None = None,
    worker_incarnation_id: str = "",
    max_streams: int = 0,
    max_srt_streams: int = 0,
    max_dash_streams: int = 0,
    max_concurrent_checks: int = 0,
    max_concurrent_deep_checks: int = 0,
    stream_budget_seconds: float = 0,
    deep_check_interval_seconds: float = 0,
    batch_budget_seconds: float = 0,
    pressure: dict | None = None,
) -> dict:
    payload = {"workerId": worker_id}
    if worker_incarnation_id:
        payload.update(
            {
                "apiVersion": WORKER_API_VERSION_V2,
                "workerIncarnationId": worker_incarnation_id,
                "softwareVersion": "videosim",
            }
        )
    capacity = {}
    if max_streams:
        capacity["maxStreams"] = max_streams
    if max_srt_streams:
        capacity["maxSrtStreams"] = max_srt_streams
    if max_dash_streams:
        capacity["maxDashStreams"] = max_dash_streams
    if max_concurrent_checks:
        capacity["maxConcurrentChecks"] = max_concurrent_checks
    if max_concurrent_deep_checks:
        capacity["maxConcurrentDeepChecks"] = max_concurrent_deep_checks
    if stream_budget_seconds:
        capacity["streamBudgetSeconds"] = stream_budget_seconds
    if deep_check_interval_seconds:
        capacity["deepCheckIntervalSeconds"] = deep_check_interval_seconds
    if batch_budget_seconds:
        capacity["batchBudgetSeconds"] = batch_budget_seconds
    if pressure is not None:
        capacity["pressure"] = dict(pressure)
    if capacity:
        payload["capacity"] = capacity
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        f"{control_plane_url.rstrip('/')}/api/workers/register",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _open(request, ssl_context) as response:
        return json.loads(response.read().decode("utf-8"))


def post_drain(
    control_plane_url: str,
    worker_id: str,
    ssl_context: ssl.SSLContext | None = None,
    worker_incarnation_id: str = "",
) -> dict:
    payload = {"workerId": worker_id}
    if worker_incarnation_id:
        payload.update(
            {
                "apiVersion": WORKER_API_VERSION_V2,
                "workerIncarnationId": worker_incarnation_id,
            }
        )
    request = Request(
        f"{control_plane_url.rstrip('/')}/api/workers/drain",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _open(request, ssl_context) as response:
        return json.loads(response.read().decode("utf-8"))


def _heartbeat_loop(
    control_plane_url: str,
    worker_id: str,
    interval_seconds: float,
    stop: threading.Event,
    ssl_context: ssl.SSLContext | None,
    worker_incarnation_id: str,
    max_streams: int,
    max_srt_streams: int,
    max_dash_streams: int,
    max_concurrent_checks: int,
    max_concurrent_deep_checks: int,
    stream_budget_seconds: float,
    deep_check_interval_seconds: float,
    batch_budget_seconds: float,
    pressure_snapshot: Callable[[], dict],
):
    while not stop.wait(interval_seconds):
        try:
            if (
                max_streams
                or max_srt_streams
                or max_dash_streams
                or max_concurrent_checks
                or max_concurrent_deep_checks
                or stream_budget_seconds
                or deep_check_interval_seconds
                or batch_budget_seconds
            ):
                post_heartbeat(
                    control_plane_url,
                    worker_id,
                    ssl_context,
                    worker_incarnation_id,
                    max_streams,
                    max_srt_streams,
                    max_dash_streams,
                    max_concurrent_checks,
                    max_concurrent_deep_checks,
                    stream_budget_seconds,
                    deep_check_interval_seconds,
                    batch_budget_seconds,
                    pressure=pressure_snapshot(),
                )
            else:
                post_heartbeat(
                    control_plane_url,
                    worker_id,
                    ssl_context,
                    worker_incarnation_id,
                    pressure=pressure_snapshot(),
                )
        except Exception:
            # A failed heartbeat is retried on the next independent interval.
            # Assignment and report calls still enforce current authority.
            continue


def post_lease_acknowledgement(
    control_plane_url: str,
    worker_id: str,
    worker_incarnation_id: str,
    assignment: dict,
    ssl_context: ssl.SSLContext | None = None,
) -> dict:
    leases = [
        {
            "streamId": stream["id"],
            "epoch": stream["lease"]["epoch"],
            "configVersion": stream["lease"]["configVersion"],
        }
        for stream in assignment.get("streams", [])
    ]
    body = json.dumps(
        {
            "apiVersion": WORKER_API_VERSION_V2,
            "workerId": worker_id,
            "workerIncarnationId": worker_incarnation_id,
            "leases": leases,
        }
    ).encode("utf-8")
    request = Request(
        f"{control_plane_url.rstrip('/')}/api/workers/leases/ack",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _open(request, ssl_context) as response:
        return json.loads(response.read().decode("utf-8"))


def build_report_payload(
    worker_id: str,
    stream_ids: list[str],
    state: dict,
    assignment: dict | None = None,
    *,
    worker_incarnation_id: str = "",
    sequence: int = 0,
    report_id: str = "",
    lease_sequences: dict[str, int] | None = None,
) -> dict:
    state = {
        key: value
        for key, value in state.items()
        if key not in {"deepCheckSchedule", "validationCursor"}
    }
    if assignment and assignment.get("apiVersion") == WORKER_API_VERSION_V2:
        if not worker_incarnation_id or sequence < 1 or not report_id:
            raise ValueError("worker API v2 report identity and sequence are required")
        lease_sequences = lease_sequences or {
            stream_id: sequence for stream_id in stream_ids
        }
        leases = [
            {
                "streamId": stream["id"],
                "epoch": stream["lease"]["epoch"],
                "configVersion": stream["lease"]["configVersion"],
                "sequence": lease_sequences[stream["id"]],
            }
            for stream in assignment.get("streams", [])
            if stream.get("id") in stream_ids
        ]
        payload = {
            "apiVersion": WORKER_API_VERSION_V2,
            "reportId": report_id,
            "workerId": worker_id,
            "workerIncarnationId": worker_incarnation_id,
            "sequence": sequence,
            "streamIds": stream_ids,
            "leases": leases,
            "state": state,
        }
    else:
        contract = {
            field: assignment[field]
            for field in CONTRACT_FIELDS
            if assignment and field in assignment
        }
        payload = {
            "workerId": worker_id,
            "streamIds": stream_ids,
            "state": state,
            **contract,
        }
    return payload


def post_report_payload(
    control_plane_url: str,
    payload: dict,
    ssl_context: ssl.SSLContext | None = None,
) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        f"{control_plane_url.rstrip('/')}/api/workers/report",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _open(request, ssl_context) as response:
        return json.loads(response.read().decode("utf-8"))


def post_report(
    control_plane_url: str,
    worker_id: str,
    stream_ids: list[str],
    state: dict,
    assignment: dict | None = None,
    ssl_context: ssl.SSLContext | None = None,
    *,
    worker_incarnation_id: str = "",
    sequence: int = 0,
    report_id: str = "",
    lease_sequences: dict[str, int] | None = None,
) -> dict:
    return post_report_payload(
        control_plane_url,
        build_report_payload(
            worker_id,
            stream_ids,
            state,
            assignment,
            worker_incarnation_id=worker_incarnation_id,
            sequence=sequence,
            report_id=report_id,
            lease_sequences=lease_sequences,
        ),
        ssl_context,
    )


def retryable_transport_error(exc: Exception) -> bool:
    if isinstance(exc, HTTPError):
        return exc.code in {429, 500, 502, 503, 504}
    return isinstance(exc, (TimeoutError, URLError))


def call_with_retry(
    operation: Callable[[], T],
    *,
    attempts: int = DEFAULT_RETRY_ATTEMPTS,
    base_seconds: float = DEFAULT_RETRY_BASE_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    random_value: Callable[[], float] = random.random,
) -> T:
    if attempts < 1:
        raise ValueError("retry attempts must be at least 1")
    if base_seconds < 0:
        raise ValueError("retry base seconds must be zero or greater")
    for attempt in range(attempts):
        try:
            return operation()
        except Exception as exc:
            if not retryable_transport_error(exc) or attempt + 1 >= attempts:
                raise
            cap = min(base_seconds * (2**attempt), 5.0)
            sleep(cap * (0.5 + max(0.0, min(1.0, random_value())) / 2))
    raise RuntimeError("retry loop exited unexpectedly")


def flush_report_spool(
    spool: EncryptedReportSpool,
    control_plane_url: str,
    ssl_context: ssl.SSLContext | None,
    retry_attempts: int,
    retry_base_seconds: float,
) -> str:
    disposition = "delivered"
    for path in spool.entries():
        payload = spool.read(path)
        try:
            call_with_retry(
                lambda: post_report_payload(control_plane_url, payload, ssl_context),
                attempts=retry_attempts,
                base_seconds=retry_base_seconds,
            )
        except HTTPError as exc:
            if exc.code == 409:
                spool.acknowledge(path)
                disposition = "stale"
                continue
            if retryable_transport_error(exc):
                return "blocked"
            raise
        except Exception as exc:
            if retryable_transport_error(exc):
                return "blocked"
            raise
        spool.acknowledge(path)
    return disposition


def advance_lease_sequences(
    streams: list[dict],
    previous: dict[tuple[str, int, int], int],
) -> tuple[dict[tuple[str, int, int], int], dict[str, int]]:
    current = {}
    report_sequences = {}
    for stream in streams:
        lease = stream.get("lease")
        if not isinstance(lease, dict):
            continue
        key = (
            stream["id"],
            int(lease["epoch"]),
            int(lease["configVersion"]),
        )
        next_sequence = previous.get(key, 0) + 1
        current[key] = next_sequence
        report_sequences[stream["id"]] = next_sequence
    return current, report_sequences


def worker_pressure_from_state(state: dict, assigned_streams: int) -> dict:
    metrics = state.get("probeMetrics", {})
    items = metrics.get("streams", []) if isinstance(metrics, dict) else []
    deferred = [
        item
        for item in items
        if isinstance(item, dict)
        and item.get("outcome") == "skipped"
        and item.get("detail") == BATCH_BUDGET_EXHAUSTED
    ]
    duration = metrics.get("batchDurationMs", 0) if isinstance(metrics, dict) else 0
    return {
        "assignedStreams": assigned_streams,
        "cycleActive": False,
        "lastValidationDeferred": sum(
            item.get("check") == "validation" for item in deferred
        ),
        "lastDeepDeferred": sum(
            item.get("check") == "deep_checks" for item in deferred
        ),
        "lastBatchDurationMs": duration,
    }


def worker_resource_snapshot() -> dict:
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    rss_scale = 1 if sys.platform == "darwin" else 1024
    snapshot = {
        "totalCpuSeconds": own.ru_utime
        + own.ru_stime
        + children.ru_utime
        + children.ru_stime,
        "processPeakRssBytes": max(0, int(own.ru_maxrss * rss_scale)),
        "childPeakRssBytes": max(0, int(children.ru_maxrss * rss_scale)),
    }
    try:
        snapshot["openFileDescriptors"] = sum(
            1 for _ in Path("/proc/self/fd").iterdir()
        )
    except OSError:
        pass
    return snapshot


def worker_resource_pressure(before: dict, after: dict) -> dict:
    pressure = {
        "lastBatchCpuMs": round(
            max(0.0, after["totalCpuSeconds"] - before["totalCpuSeconds"])
            * 1000,
            3,
        ),
        "processPeakRssBytes": after["processPeakRssBytes"],
        "childPeakRssBytes": after["childPeakRssBytes"],
    }
    if "openFileDescriptors" in after:
        pressure["openFileDescriptors"] = after["openFileDescriptors"]
    return pressure


def run_worker(
    control_plane_url: str,
    worker_id: str,
    poll_seconds: float,
    repeat_seconds: float,
    history_limit: int,
    srt_host: str,
    once: bool = False,
    heartbeat_seconds: float = 20.0,
    ssl_context: ssl.SSLContext | None = None,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    retry_base_seconds: float = DEFAULT_RETRY_BASE_SECONDS,
    max_streams: int = 0,
    max_srt_streams: int = 0,
    max_dash_streams: int = 0,
    max_concurrent_checks: int = 1,
    max_concurrent_deep_checks: int = 0,
    stream_budget_seconds: float = 0,
    deep_check_interval_seconds: float = 0,
    batch_budget_seconds: float = 0,
    report_spool_dir: str = "",
    report_spool_key_file: str = "",
    report_spool_max_bytes: int = 0,
    drain_event: threading.Event | None = None,
) -> int:
    if heartbeat_seconds <= 0:
        raise ValueError("heartbeat_seconds must be greater than 0")
    if max_streams < 0:
        raise ValueError("max_streams must be zero or greater")
    if max_srt_streams < 0:
        raise ValueError("max_srt_streams must be zero or greater")
    if max_dash_streams < 0:
        raise ValueError("max_dash_streams must be zero or greater")
    if max_concurrent_checks < 1:
        raise ValueError("max_concurrent_checks must be at least 1")
    if max_concurrent_deep_checks < 0:
        raise ValueError("max_concurrent_deep_checks must be zero or greater")
    if stream_budget_seconds < 0:
        raise ValueError("stream_budget_seconds must be zero or greater")
    if not math.isfinite(deep_check_interval_seconds) or deep_check_interval_seconds < 0:
        raise ValueError("deep_check_interval_seconds must be zero or greater")
    if not math.isfinite(batch_budget_seconds) or batch_budget_seconds < 0:
        raise ValueError("batch_budget_seconds must be zero or greater")
    spool_options = (
        bool(report_spool_dir),
        bool(report_spool_key_file),
        isinstance(report_spool_max_bytes, int)
        and not isinstance(report_spool_max_bytes, bool)
        and report_spool_max_bytes > 0,
    )
    if any(spool_options) and not all(spool_options):
        raise ValueError(
            "report spool directory, key file, and positive max bytes are required together"
        )
    report_spool = (
        EncryptedReportSpool(
            report_spool_dir, report_spool_key_file, report_spool_max_bytes
        )
        if all(spool_options)
        else None
    )
    spool_blocked = False
    spool_stats = report_spool.stats() if report_spool is not None else {}
    initial_resources = worker_resource_snapshot()
    pressure_lock = threading.Lock()
    pressure = {
        "assignedStreams": 0,
        "cycleActive": False,
        "lastValidationDeferred": 0,
        "lastDeepDeferred": 0,
        "lastBatchDurationMs": 0,
        "lastBatchCpuMs": 0,
        "processPeakRssBytes": initial_resources["processPeakRssBytes"],
        "childPeakRssBytes": initial_resources["childPeakRssBytes"],
        "spoolBlocked": False,
        "spoolQueuedReports": int(spool_stats.get("queuedReports", 0)),
        "spoolBytes": int(spool_stats.get("bytes", 0)),
    }
    if "openFileDescriptors" in initial_resources:
        pressure["openFileDescriptors"] = initial_resources[
            "openFileDescriptors"
        ]

    def update_pressure(**values):
        with pressure_lock:
            pressure.update(values)

    def pressure_snapshot() -> dict:
        with pressure_lock:
            return dict(pressure)

    def note_spool_pressure(blocked: bool):
        nonlocal spool_blocked
        if report_spool is None:
            return
        stats = report_spool.stats()
        update_pressure(
            spoolBlocked=blocked,
            spoolQueuedReports=stats["queuedReports"],
            spoolBytes=stats["bytes"],
        )
        if blocked == spool_blocked:
            return
        spool_blocked = blocked
        print(
            f"[videosim-worker] report_spool={'blocked' if blocked else 'recovered'} "
            f"queued_reports={stats['queuedReports']} bytes={stats['bytes']} "
            f"max_bytes={stats['maxBytes']}",
            flush=True,
        )

    drain_requested = drain_event or threading.Event()
    while report_spool is not None and report_spool.entries():
        replay = flush_report_spool(
            report_spool,
            control_plane_url,
            ssl_context,
            retry_attempts,
            retry_base_seconds,
        )
        note_spool_pressure(replay == "blocked")
        if replay != "blocked":
            continue
        if once:
            return 1
        if drain_requested.wait(poll_seconds):
            return 0
    state = empty_monitor_state()
    assignment_conflicts = 0
    report_sequence = 0
    lease_sequences: dict[tuple[str, int, int], int] = {}
    worker_incarnation_id = str(uuid.uuid4())
    previous_signal_handlers = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_signal_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, lambda _signum, _frame: drain_requested.set())
    heartbeat_stop = threading.Event()
    heartbeat = threading.Thread(
        target=_heartbeat_loop,
        args=(
            control_plane_url,
            worker_id,
            heartbeat_seconds,
            heartbeat_stop,
            ssl_context,
            worker_incarnation_id,
            max_streams,
            max_srt_streams,
            max_dash_streams,
            max_concurrent_checks if max_concurrent_checks > 1 else 0,
            max_concurrent_deep_checks,
            stream_budget_seconds,
            deep_check_interval_seconds,
            batch_budget_seconds,
            pressure_snapshot,
        ),
        daemon=True,
        name=f"videosim-heartbeat-{worker_id}",
    )
    heartbeat.start()
    registered = False
    drain_sent = False

    def finish_drain():
        nonlocal drain_sent
        heartbeat_stop.set()
        heartbeat.join(timeout=min(heartbeat_seconds, 1.0))
        if registered and not drain_sent:
            call_with_retry(
                lambda: post_drain(
                    control_plane_url,
                    worker_id,
                    ssl_context,
                    worker_incarnation_id,
                ),
                attempts=retry_attempts,
                base_seconds=retry_base_seconds,
            )
            drain_sent = True

    try:
        if (
            max_streams
            or max_srt_streams
            or max_dash_streams
            or max_concurrent_checks > 1
            or max_concurrent_deep_checks
            or stream_budget_seconds
            or deep_check_interval_seconds
            or batch_budget_seconds
        ):
            call_with_retry(
                lambda: post_heartbeat(
                    control_plane_url,
                    worker_id,
                    ssl_context,
                    worker_incarnation_id,
                    max_streams,
                    max_srt_streams,
                    max_dash_streams,
                    max_concurrent_checks if max_concurrent_checks > 1 else 0,
                    max_concurrent_deep_checks,
                    stream_budget_seconds,
                    deep_check_interval_seconds,
                    batch_budget_seconds,
                    pressure=pressure_snapshot(),
                ),
                attempts=retry_attempts,
                base_seconds=retry_base_seconds,
            )
            registered = True
        while True:
            if drain_requested.is_set():
                finish_drain()
                return 0
            if report_spool is not None:
                replay = flush_report_spool(
                    report_spool,
                    control_plane_url,
                    ssl_context,
                    retry_attempts,
                    retry_base_seconds,
                )
                note_spool_pressure(replay == "blocked")
                if replay == "blocked":
                    if once:
                        return 1
                    if drain_requested.wait(poll_seconds):
                        finish_drain()
                        return 0
                    continue
                if replay == "stale":
                    state = empty_monitor_state()
            try:
                assignments = call_with_retry(
                    lambda: fetch_assignments(
                        control_plane_url,
                        worker_id,
                        ssl_context,
                        worker_incarnation_id,
                    ),
                    attempts=retry_attempts,
                    base_seconds=retry_base_seconds,
                )
            except Exception as exc:
                if not retryable_transport_error(exc):
                    raise
                update_pressure(cycleActive=False)
                if once:
                    return 1
                if drain_requested.wait(poll_seconds):
                    finish_drain()
                    return 0
                continue
            registered = True
            streams = assignments.get("streams", [])
            if (
                report_spool is not None
                and assignments.get("apiVersion") != WORKER_API_VERSION_V2
            ):
                raise ReportSpoolError(
                    "encrypted report spooling requires worker API v2 assignments"
                )
            if assignments.get("apiVersion") == WORKER_API_VERSION_V2:
                try:
                    call_with_retry(
                        lambda: post_lease_acknowledgement(
                            control_plane_url,
                            worker_id,
                            worker_incarnation_id,
                            assignments,
                            ssl_context,
                        ),
                        attempts=retry_attempts,
                        base_seconds=retry_base_seconds,
                    )
                except Exception as exc:
                    if isinstance(exc, HTTPError) and exc.code == 409:
                        assignment_conflicts += 1
                        state = empty_monitor_state()
                        if assignment_conflicts >= MAX_ASSIGNMENT_CONFLICT_REFETCHES:
                            raise
                        continue
                    if not retryable_transport_error(exc):
                        raise
                    update_pressure(cycleActive=False)
                    if once:
                        return 1
                    if drain_requested.wait(poll_seconds):
                        finish_drain()
                        return 0
                    continue
            if drain_requested.is_set():
                finish_drain()
                return 0
            stream_ids = {stream["id"] for stream in streams}
            state = monitor_state_for_stream_ids(state, stream_ids)
            update_pressure(assignedStreams=len(streams), cycleActive=True)
            monitor_kwargs = {}
            if max_concurrent_checks > 1:
                monitor_kwargs["max_concurrency"] = max_concurrent_checks
            if max_concurrent_deep_checks:
                monitor_kwargs["max_deep_concurrency"] = (
                    max_concurrent_deep_checks
                )
            if stream_budget_seconds:
                monitor_kwargs["stream_budget_seconds"] = stream_budget_seconds
            if deep_check_interval_seconds:
                monitor_kwargs["deep_check_interval_seconds"] = (
                    deep_check_interval_seconds
                )
            if batch_budget_seconds:
                monitor_kwargs["batch_budget_seconds"] = batch_budget_seconds
            resources_before = worker_resource_snapshot()
            state = run_monitor_once(
                {"streams": streams},
                state,
                time.time(),
                repeat_seconds,
                history_limit,
                srt_host,
                **monitor_kwargs,
            )
            state = monitor_state_for_stream_ids(state, stream_ids)
            update_pressure(
                **worker_pressure_from_state(state, len(streams)),
                **worker_resource_pressure(
                    resources_before, worker_resource_snapshot()
                ),
            )
            report_sequence += 1
            lease_sequences, report_lease_sequences = advance_lease_sequences(
                streams, lease_sequences
            )
            report_id = str(uuid.uuid4())
            report_conflict = False
            report_conflict_error = None
            try:
                if report_spool is not None:
                    report_spool.enqueue(
                        build_report_payload(
                            worker_id,
                            sorted(stream_ids),
                            state,
                            assignments,
                            worker_incarnation_id=worker_incarnation_id,
                            sequence=report_sequence,
                            report_id=report_id,
                            lease_sequences=report_lease_sequences,
                        )
                    )
                    delivery = flush_report_spool(
                        report_spool,
                        control_plane_url,
                        ssl_context,
                        retry_attempts,
                        retry_base_seconds,
                    )
                    note_spool_pressure(delivery == "blocked")
                    if delivery == "blocked":
                        if once:
                            return 1
                        continue
                    report_conflict = delivery == "stale"
                else:
                    call_with_retry(
                        lambda: post_report(
                            control_plane_url,
                            worker_id,
                            sorted(stream_ids),
                            state,
                            assignments,
                            ssl_context,
                            worker_incarnation_id=worker_incarnation_id,
                            sequence=report_sequence,
                            report_id=report_id,
                            lease_sequences=report_lease_sequences,
                        ),
                        attempts=retry_attempts,
                        base_seconds=retry_base_seconds,
                    )
            except HTTPError as exc:
                if exc.code != 409:
                    raise
                report_conflict = True
                report_conflict_error = exc
            if report_conflict:
                assignment_conflicts += 1
                state = empty_monitor_state()
                if assignment_conflicts >= MAX_ASSIGNMENT_CONFLICT_REFETCHES:
                    if report_conflict_error is not None:
                        raise report_conflict_error
                    raise ReportSpoolError(
                        "report spool exceeded assignment conflict refetch limit"
                    )
                continue
            assignment_conflicts = 0
            if drain_requested.is_set():
                finish_drain()
                return 0
            if once:
                return 0
            if drain_requested.wait(poll_seconds):
                finish_drain()
                return 0
    finally:
        heartbeat_stop.set()
        heartbeat.join(timeout=min(heartbeat_seconds, 1.0))
        if drain_requested.is_set() and registered and not drain_sent:
            try:
                post_drain(
                    control_plane_url,
                    worker_id,
                    ssl_context,
                    worker_incarnation_id,
                )
            except Exception:
                pass
        for signum, handler in previous_signal_handlers.items():
            signal.signal(signum, handler)
