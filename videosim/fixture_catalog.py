from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse

from .fixture_scenario import load_fixture_scenario_state
from .gui import FeedRecord, stream_registration


def fixture_feed_configs(state: dict) -> list[dict]:
    configs = []
    for stream in state["streams"]:
        endpoint = urlparse(stream["endpoint"])
        port = endpoint.port or (443 if endpoint.scheme == "https" else 80)
        configs.append(
            stream_registration(
                FeedRecord(
                    id=stream["id"],
                    name=stream["name"],
                    source="external",
                    external_url=stream["endpoint"],
                    desired_state="running",
                    protocol=stream["protocol"],
                    mode="normal",
                    http_port=8080,
                    feed_port=port,
                    width=stream["width"],
                    height=stream["height"],
                    framerate=stream["framerate"],
                    dash_dir=f"/tmp/videosim-dash/{stream['id']}",
                    alert_enabled_ids=None,
                    alert_delay_seconds=0,
                )
            )
        )
    return sorted(configs, key=lambda item: item["id"])


def load_fixture_feed_configs(
    path: str | Path, *, require_distinct_endpoints: bool = False
) -> tuple[list[dict], dict, str]:
    state, digest = load_fixture_scenario_state(
        path, require_distinct_endpoints=require_distinct_endpoints
    )
    return fixture_feed_configs(state), state, digest


def fixture_config_sha256(configs: list[dict]) -> str:
    payload = json.dumps(
        sorted(configs, key=lambda item: item["id"]),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
