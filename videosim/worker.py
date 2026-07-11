from __future__ import annotations

import json
import socket
import threading
import time
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .monitor import empty_monitor_state, monitor_state_for_stream_ids, run_monitor_once


MAX_ASSIGNMENT_CONFLICT_REFETCHES = 3
CONTRACT_FIELDS = (
    "apiVersion",
    "controlPlaneInstanceId",
    "assignmentGeneration",
    "assignmentToken",
)


def default_worker_id() -> str:
    return socket.gethostname()


def fetch_assignments(control_plane_url: str, worker_id: str) -> dict:
    query = urlencode({"worker_id": worker_id})
    with urlopen(f"{control_plane_url.rstrip('/')}/api/workers/assignments?{query}", timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def post_heartbeat(control_plane_url: str, worker_id: str) -> dict:
    body = json.dumps({"workerId": worker_id}).encode("utf-8")
    request = Request(
        f"{control_plane_url.rstrip('/')}/api/workers/register",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _heartbeat_loop(control_plane_url: str, worker_id: str, interval_seconds: float, stop: threading.Event):
    while not stop.wait(interval_seconds):
        try:
            post_heartbeat(control_plane_url, worker_id)
        except Exception:
            # Assignment/report calls remain authoritative. A later reliability
            # milestone records heartbeat health and adds bounded backoff.
            continue


def post_report(
    control_plane_url: str,
    worker_id: str,
    stream_ids: list[str],
    state: dict,
    assignment: dict | None = None,
) -> dict:
    contract = {field: assignment[field] for field in CONTRACT_FIELDS if assignment and field in assignment}
    body = json.dumps({"workerId": worker_id, "streamIds": stream_ids, "state": state, **contract}).encode("utf-8")
    request = Request(
        f"{control_plane_url.rstrip('/')}/api/workers/report",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def run_worker(
    control_plane_url: str,
    worker_id: str,
    poll_seconds: float,
    repeat_seconds: float,
    history_limit: int,
    srt_host: str,
    once: bool = False,
    heartbeat_seconds: float = 20.0,
) -> int:
    if heartbeat_seconds <= 0:
        raise ValueError("heartbeat_seconds must be greater than 0")
    state = empty_monitor_state()
    assignment_conflicts = 0
    heartbeat_stop = threading.Event()
    heartbeat = threading.Thread(
        target=_heartbeat_loop,
        args=(control_plane_url, worker_id, heartbeat_seconds, heartbeat_stop),
        daemon=True,
        name=f"videosim-heartbeat-{worker_id}",
    )
    heartbeat.start()
    try:
        while True:
            assignments = fetch_assignments(control_plane_url, worker_id)
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
                post_report(control_plane_url, worker_id, sorted(stream_ids), state, assignments)
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
