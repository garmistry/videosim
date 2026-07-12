from __future__ import annotations

import json
import random
import signal
import socket
import ssl
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .monitor import empty_monitor_state, monitor_state_for_stream_ids, run_monitor_once


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
    max_concurrent_checks: int = 0,
    stream_budget_seconds: float = 0,
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
    if max_concurrent_checks:
        capacity["maxConcurrentChecks"] = max_concurrent_checks
    if stream_budget_seconds:
        capacity["streamBudgetSeconds"] = stream_budget_seconds
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
    max_concurrent_checks: int,
    stream_budget_seconds: float,
):
    while not stop.wait(interval_seconds):
        try:
            if max_streams or max_concurrent_checks or stream_budget_seconds:
                post_heartbeat(
                    control_plane_url,
                    worker_id,
                    ssl_context,
                    worker_incarnation_id,
                    max_streams,
                    max_concurrent_checks,
                    stream_budget_seconds,
                )
            else:
                post_heartbeat(
                    control_plane_url, worker_id, ssl_context, worker_incarnation_id
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
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        f"{control_plane_url.rstrip('/')}/api/workers/report",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _open(request, ssl_context) as response:
        return json.loads(response.read().decode("utf-8"))


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
    max_concurrent_checks: int = 1,
    stream_budget_seconds: float = 0,
    drain_event: threading.Event | None = None,
) -> int:
    if heartbeat_seconds <= 0:
        raise ValueError("heartbeat_seconds must be greater than 0")
    if max_streams < 0:
        raise ValueError("max_streams must be zero or greater")
    if max_concurrent_checks < 1:
        raise ValueError("max_concurrent_checks must be at least 1")
    if stream_budget_seconds < 0:
        raise ValueError("stream_budget_seconds must be zero or greater")
    state = empty_monitor_state()
    assignment_conflicts = 0
    report_sequence = 0
    lease_sequences: dict[tuple[str, int, int], int] = {}
    worker_incarnation_id = str(uuid.uuid4())
    drain_requested = drain_event or threading.Event()
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
            max_concurrent_checks if max_concurrent_checks > 1 else 0,
            stream_budget_seconds,
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
        if max_streams or max_concurrent_checks > 1 or stream_budget_seconds:
            call_with_retry(
                lambda: post_heartbeat(
                    control_plane_url,
                    worker_id,
                    ssl_context,
                    worker_incarnation_id,
                    max_streams,
                    max_concurrent_checks if max_concurrent_checks > 1 else 0,
                    stream_budget_seconds,
                ),
                attempts=retry_attempts,
                base_seconds=retry_base_seconds,
            )
            registered = True
        while True:
            if drain_requested.is_set():
                finish_drain()
                return 0
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
            registered = True
            streams = assignments.get("streams", [])
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
                except HTTPError as exc:
                    if exc.code != 409:
                        raise
                    assignment_conflicts += 1
                    state = empty_monitor_state()
                    if assignment_conflicts >= MAX_ASSIGNMENT_CONFLICT_REFETCHES:
                        raise
                    continue
            if drain_requested.is_set():
                finish_drain()
                return 0
            stream_ids = {stream["id"] for stream in streams}
            state = monitor_state_for_stream_ids(state, stream_ids)
            monitor_kwargs = {}
            if max_concurrent_checks > 1:
                monitor_kwargs["max_concurrency"] = max_concurrent_checks
            if stream_budget_seconds:
                monitor_kwargs["stream_budget_seconds"] = stream_budget_seconds
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
            report_sequence += 1
            lease_sequences, report_lease_sequences = advance_lease_sequences(
                streams, lease_sequences
            )
            report_id = str(uuid.uuid4())
            try:
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
                assignment_conflicts += 1
                state = empty_monitor_state()
                if assignment_conflicts >= MAX_ASSIGNMENT_CONFLICT_REFETCHES:
                    raise
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
