from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse

from .fixture_fleet import write_fixture_state
from .fixture_scenario import load_fixture_scenario_state
from .gui import FeedRecord, stream_registration
from .postgres_store import PostgresControlPlaneStore, PostgresStoreError


CATALOG_IMPORT_SCHEMA = "videosim.fixture-catalog-import/v1"


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


def run_fixture_catalog_import(
    database_url: str,
    state_path: str | Path,
    output_path: str | Path,
    *,
    require_distinct_endpoints: bool = True,
    store_factory=PostgresControlPlaneStore,
) -> dict:
    configs, state, state_sha256 = load_fixture_feed_configs(
        state_path, require_distinct_endpoints=require_distinct_endpoints
    )
    store = store_factory(database_url)
    try:
        changed = store.import_feeds_if_changed(configs, reject_unlisted=True)
        persisted = store.load()
    finally:
        store.close()

    versions = [int(item["config_version"]) for item in persisted]
    persisted_configs = []
    for item in persisted:
        config = dict(item)
        config.pop("config_version", None)
        persisted_configs.append(config)
    persisted_configs.sort(key=lambda item: item["id"])
    if persisted_configs != configs:
        raise PostgresStoreError(
            "persisted fixture catalog does not exactly match the scenario"
        )

    endpoint_count = len({stream["endpoint"] for stream in state["streams"]})
    report = {
        "schemaVersion": CATALOG_IMPORT_SCHEMA,
        "passed": True,
        "sourceStateSha256": state_sha256,
        "catalogConfigSha256": fixture_config_sha256(configs),
        "streamsInspected": len(configs),
        "feedsChanged": changed,
        "feedsUnchanged": len(configs) - changed,
        "protocolCounts": state["protocolCounts"],
        "behaviorCounts": state["behaviorCounts"],
        "distinctEndpointCount": endpoint_count,
        "allEndpointsDistinct": endpoint_count == len(configs),
        "minimumConfigVersion": min(versions),
        "maximumConfigVersion": max(versions),
        "catalogExclusive": True,
        "capacityCertified": False,
        "allEndpointMediaValidated": False,
    }
    write_fixture_state(output_path, report)
    print(
        f"Fixture catalog imported: changed={changed} inspected={len(configs)} "
        f"state={state_path} report={output_path}",
        flush=True,
    )
    return report
