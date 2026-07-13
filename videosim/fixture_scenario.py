from __future__ import annotations

import hashlib
import json
import math
import random
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from .control_plane import MAX_REPORT_STREAMS
from .fixture_fleet import BEHAVIORS, write_fixture_state


SCENARIO_SCHEMA = "videosim.fixture-scenario/v1"
PROTOCOLS = ("srt", "dash")


def _read_object(path: str | Path, label: str) -> tuple[dict, str]:
    source = Path(path)
    try:
        raw = source.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is invalid: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value, hashlib.sha256(raw).hexdigest()


def _validate_mix(value: object, fields: tuple[str, ...], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError(f"{label} must contain exactly {', '.join(fields)}")
    for field in fields:
        percent = value[field]
        if (
            isinstance(percent, bool)
            or not isinstance(percent, (int, float))
            or not math.isfinite(percent)
            or percent < 0
        ):
            raise ValueError(f"{label}.{field} must be a finite non-negative number")
    if not math.isclose(sum(value.values()), 100, abs_tol=1e-6):
        raise ValueError(f"{label} percentages must total 100")
    return value


def load_fixture_scenario_manifest(path: str | Path) -> tuple[dict, str]:
    manifest, digest = _read_object(path, "fixture scenario manifest")
    if manifest.get("schemaVersion") != SCENARIO_SCHEMA:
        raise ValueError(f"fixture scenario schemaVersion must be {SCENARIO_SCHEMA}")
    stream_count = manifest.get("streamCount")
    if (
        isinstance(stream_count, bool)
        or not isinstance(stream_count, int)
        or not 1 <= stream_count <= MAX_REPORT_STREAMS
    ):
        raise ValueError(
            f"fixture scenario streamCount must be an integer from 1 to {MAX_REPORT_STREAMS}"
        )
    seed = manifest.get("randomSeed")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("fixture scenario randomSeed must be a non-negative integer")
    _validate_mix(manifest.get("protocolPercent"), PROTOCOLS, "protocolPercent")
    _validate_mix(
        manifest.get("behaviorPercent"), BEHAVIORS, "behaviorPercent"
    )
    return manifest, digest


def _allocate(total: int, percentages: dict, fields: tuple[str, ...]) -> dict[str, int]:
    exact = {field: total * percentages[field] / 100 for field in fields}
    counts = {field: math.floor(exact[field]) for field in fields}
    remainder = total - sum(counts.values())
    order = sorted(
        fields,
        key=lambda field: (exact[field] - counts[field], -fields.index(field)),
        reverse=True,
    )
    for field in order[:remainder]:
        counts[field] += 1
    return counts


def load_protocol_fixture_state(path: str | Path, protocol: str) -> tuple[dict, str]:
    state, digest = _read_object(path, f"{protocol} fixture state")
    if state.get("schemaVersion") != "videosim.fixture-state/v1":
        raise ValueError(f"{protocol} fixture state has an unsupported schemaVersion")
    streams = state.get("streams")
    if not isinstance(streams, list):
        raise ValueError(f"{protocol} fixture state must contain a streams array")
    fixtures = {behavior: [] for behavior in BEHAVIORS}
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        behavior = stream.get("fixtureBehavior")
        if behavior not in BEHAVIORS:
            behavior = next(
                (
                    item
                    for item in BEHAVIORS
                    if stream.get("id") == f"{protocol}-{item}"
                ),
                None,
            )
        if behavior is None:
            continue
        if (
            not isinstance(stream.get("id"), str)
            or not stream["id"]
            or stream.get("protocol") != protocol
            or stream.get("source") != "external"
            or stream.get("status") != "running"
            or not isinstance(stream.get("endpoint"), str)
            or not stream["endpoint"]
        ):
            raise ValueError(
                f"{protocol} fixture state must contain running external {behavior}"
            )
        fixtures[behavior].append(stream)
    for behavior in BEHAVIORS:
        if not fixtures[behavior]:
            raise ValueError(
                f"{protocol} fixture state must contain running external {behavior}"
            )
        fixtures[behavior].sort(key=lambda stream: stream["id"])
    return fixtures, digest


def load_protocol_fixture_states(
    paths: str | Path | Sequence[str | Path], protocol: str
) -> tuple[dict[str, list[dict]], tuple[str, ...]]:
    state_paths = [paths] if isinstance(paths, (str, Path)) else list(paths)
    if not state_paths:
        raise ValueError(f"{protocol} fixture states must not be empty")
    combined = {behavior: [] for behavior in BEHAVIORS}
    digests = []
    prior_endpoints = set()
    for path in state_paths:
        fixtures, digest = load_protocol_fixture_state(path, protocol)
        state_endpoints = {
            stream["endpoint"]
            for behavior in BEHAVIORS
            for stream in fixtures[behavior]
        }
        overlap = prior_endpoints & state_endpoints
        if overlap:
            raise ValueError(
                f"{protocol} fixture states contain duplicate endpoint {min(overlap)}"
            )
        prior_endpoints.update(state_endpoints)
        digests.append(digest)
        for behavior in BEHAVIORS:
            combined[behavior].extend(fixtures[behavior])
    for behavior in BEHAVIORS:
        combined[behavior].sort(key=lambda stream: (stream["endpoint"], stream["id"]))
    return combined, tuple(digests)


def combined_fixture_digest(digests: tuple[str, ...]) -> str:
    if len(digests) == 1:
        return digests[0]
    payload = json.dumps(digests, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def compose_fixture_scenario(
    manifest: dict,
    manifest_sha256: str,
    fixtures: dict[str, dict[str, list[dict]]],
    fixture_digests: dict[str, tuple[str, ...]],
) -> dict:
    protocol_counts = _allocate(
        manifest["streamCount"], manifest["protocolPercent"], PROTOCOLS
    )
    streams = []
    behavior_counts = {behavior: 0 for behavior in BEHAVIORS}
    for protocol in PROTOCOLS:
        counts = _allocate(
            protocol_counts[protocol], manifest["behaviorPercent"], BEHAVIORS
        )
        for behavior in BEHAVIORS:
            behavior_counts[behavior] += counts[behavior]
            candidates = fixtures[protocol][behavior]
            for index in range(counts[behavior]):
                stream = dict(candidates[index % len(candidates)])
                stream["fixtureBehavior"] = behavior
                streams.append(stream)
    random.Random(manifest["randomSeed"]).shuffle(streams)
    endpoint_counts = Counter(
        (stream["protocol"], stream["endpoint"]) for stream in streams
    )
    for index, stream in enumerate(streams, start=1):
        stream["fixtureEndpointShared"] = (
            endpoint_counts[(stream["protocol"], stream["endpoint"])] > 1
        )
        stream["id"] = f"fixture-{index:05d}"
        stream["name"] = f"Fixture {index:05d} ({stream['protocol']} {stream['fixtureBehavior']})"
    shared_endpoints = any(count > 1 for count in endpoint_counts.values())
    return {
        "schemaVersion": "videosim.fixture-scenario-state/v1",
        "fixtureScenarioSha256": manifest_sha256,
        "fixtureStateSha256": {
            protocol: combined_fixture_digest(fixture_digests[protocol])
            for protocol in PROTOCOLS
        },
        "fixtureStateSha256s": {
            protocol: list(fixture_digests[protocol]) for protocol in PROTOCOLS
        },
        "fixtureStateCounts": {
            protocol: len(fixture_digests[protocol]) for protocol in PROTOCOLS
        },
        "logicalStreamsShareEndpoints": shared_endpoints,
        "scenarioLimitations": [
            (
                f"Logical streams reuse {len(endpoint_counts)} protocol endpoints."
                if shared_endpoints
                else "Each logical stream uses a distinct protocol endpoint URL."
            ),
            "Endpoint URL uniqueness does not certify independent media generation or capacity.",
        ],
        "protocolCounts": protocol_counts,
        "behaviorCounts": behavior_counts,
        "streams": streams,
    }


def run_fixture_scenario(
    manifest_path: str,
    srt_state_paths: str | Path | Sequence[str | Path],
    dash_state_paths: str | Path | Sequence[str | Path],
    state_path: str,
) -> int:
    manifest, manifest_digest = load_fixture_scenario_manifest(manifest_path)
    fixtures = {}
    digests = {}
    for protocol, paths in (("srt", srt_state_paths), ("dash", dash_state_paths)):
        fixtures[protocol], digests[protocol] = load_protocol_fixture_states(
            paths, protocol
        )
    state = compose_fixture_scenario(manifest, manifest_digest, fixtures, digests)
    write_fixture_state(state_path, state)
    print(
        f"Fixture scenario ready: streams={len(state['streams'])} state={state_path}",
        flush=True,
    )
    return 0
