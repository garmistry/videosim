from __future__ import annotations

import json
import random
import socket
import ssl
import threading
import time
from pathlib import Path
from typing import Callable, TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .monitor import empty_monitor_state, monitor_state_for_stream_ids, run_monitor_once


MAX_ASSIGNMENT_CONFLICT_REFETCHES = 3
DEFAULT_RETRY_ATTEMPTS = 5
DEFAULT_RETRY_BASE_SECONDS = 0.25
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
) -> dict:
    query = urlencode({"worker_id": worker_id})
    with _open(f"{control_plane_url.rstrip('/')}/api/workers/assignments?{query}", ssl_context) as response:
        return json.loads(response.read().decode("utf-8"))


def post_heartbeat(
    control_plane_url: str,
    worker_id: str,
    ssl_context: ssl.SSLContext | None = None,
) -> dict:
    body = json.dumps({"workerId": worker_id}).encode("utf-8")
    request = Request(
        f"{control_plane_url.rstrip('/')}/api/workers/register",
        data=body,
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
):
    while not stop.wait(interval_seconds):
        try:
            post_heartbeat(control_plane_url, worker_id, ssl_context)
        except Exception:
            # A failed heartbeat is retried on the next independent interval.
            # Assignment and report calls still enforce current authority.
            continue


def post_report(
    control_plane_url: str,
    worker_id: str,
    stream_ids: list[str],
    state: dict,
    assignment: dict | None = None,
    ssl_context: ssl.SSLContext | None = None,
) -> dict:
    contract = {field: assignment[field] for field in CONTRACT_FIELDS if assignment and field in assignment}
    body = json.dumps({"workerId": worker_id, "streamIds": stream_ids, "state": state, **contract}).encode("utf-8")
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
) -> int:
    if heartbeat_seconds <= 0:
        raise ValueError("heartbeat_seconds must be greater than 0")
    state = empty_monitor_state()
    assignment_conflicts = 0
    heartbeat_stop = threading.Event()
    heartbeat = threading.Thread(
        target=_heartbeat_loop,
        args=(control_plane_url, worker_id, heartbeat_seconds, heartbeat_stop, ssl_context),
        daemon=True,
        name=f"videosim-heartbeat-{worker_id}",
    )
    heartbeat.start()
    try:
        while True:
            assignments = call_with_retry(
                lambda: fetch_assignments(control_plane_url, worker_id, ssl_context),
                attempts=retry_attempts,
                base_seconds=retry_base_seconds,
            )
            streams = assignments.get("streams", [])
            stream_ids = {stream["id"] for stream in streams}
            state = monitor_state_for_stream_ids(state, stream_ids)
            state = run_monitor_once(
                {"streams": streams},
                state,
                time.time(),
                repeat_seconds,
                history_limit,
                srt_host,
            )
            state = monitor_state_for_stream_ids(state, stream_ids)
            try:
                call_with_retry(
                    lambda: post_report(
                        control_plane_url,
                        worker_id,
                        sorted(stream_ids),
                        state,
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
            assignment_conflicts = 0
            if once:
                return 0
            time.sleep(poll_seconds)
    finally:
        heartbeat_stop.set()
        heartbeat.join(timeout=min(heartbeat_seconds, 1.0))
