from __future__ import annotations

import json
import socket
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .monitor import empty_monitor_state, run_monitor_once


def default_worker_id() -> str:
    return socket.gethostname()


def fetch_assignments(control_plane_url: str, worker_id: str) -> dict:
    query = urlencode({"worker_id": worker_id})
    with urlopen(f"{control_plane_url.rstrip('/')}/api/workers/assignments?{query}", timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def post_report(control_plane_url: str, worker_id: str, stream_ids: list[str], state: dict) -> dict:
    body = json.dumps({"workerId": worker_id, "streamIds": stream_ids, "state": state}).encode("utf-8")
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
) -> int:
    state = empty_monitor_state()
    while True:
        assignments = fetch_assignments(control_plane_url, worker_id)
        streams = assignments.get("streams", [])
        state = run_monitor_once(
            {"streams": streams},
            state,
            time.time(),
            repeat_seconds,
            history_limit,
            srt_host,
        )
        post_report(control_plane_url, worker_id, [stream["id"] for stream in streams], state)
        if once:
            return 0
        time.sleep(poll_seconds)
