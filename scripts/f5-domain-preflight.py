#!/usr/bin/env python3
"""Verify that collected F5 fixture and worker domains match the candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from videosim.fixture_fleet import BEHAVIORS
from videosim.fixture_scenario import load_protocol_fixture_state


REPORT_SCHEMA = "videosim.f5-domain-preflight/v1"
WORKLOAD_SCHEMA = "videosim.scale-workload/v1"
TARGET_STREAMS = 1000
MINIMUM_HEADROOM_PERCENT = 30
MINIMUM_DURATION_SECONDS = 86400
PROTOCOLS = ("srt", "dash")
IMAGE_DIGEST = re.compile(r"^.+@(sha256:[0-9a-f]{64})$")
FIXTURE_CHECKS = {
    "linux_docker_engine",
    "compose_rendered",
    "fixture_services_running",
    "fixture_inventory_validated",
    "dash_health_api",
    "srt_dash_media_validated",
    "docker_resources_captured",
    "docker_logs_clean",
}
WORKER_CHECKS = {
    "candidate_configuration_validated",
    "worker_certificates_validated",
    "linux_docker_engine",
    "compose_rendered",
    "worker_services_running",
    "control_plane_health_api",
    "worker_mtls_health_api",
    "worker_services_stable",
    "docker_logs_clean",
}


def _read_object(path: str | Path, label: str) -> tuple[dict, str]:
    source = Path(path)
    try:
        raw = source.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is invalid: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value, hashlib.sha256(raw).hexdigest()


def _image_digest(value: object, label: str, errors: list[str]) -> str | None:
    match = IMAGE_DIGEST.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        errors.append(f"{label} must use an immutable @sha256 digest")
        return None
    return match.group(1)


def _validate_window(
    value: dict, label: str, errors: list[str]
) -> tuple[datetime, datetime] | None:
    timestamps = []
    for field in ("startedAt", "endedAt"):
        raw = value.get(field)
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            errors.append(f"{label}.{field} must be an ISO-8601 timestamp")
            return None
        if parsed.tzinfo is None:
            errors.append(f"{label}.{field} must include a timezone")
            return None
        timestamps.append(parsed)
    if timestamps[1] <= timestamps[0]:
        errors.append(f"{label}.endedAt must be after startedAt")
        return None
    return timestamps[0], timestamps[1]


def _expected_count(
    total: int, percentage: object, label: str, errors: list[str]
) -> int:
    if (
        isinstance(percentage, bool)
        or not isinstance(percentage, (int, float))
        or not math.isfinite(percentage)
        or percentage < 0
    ):
        errors.append(f"{label} must be a finite non-negative number")
        return 0
    exact = total * percentage / 100
    if not exact.is_integer():
        errors.append(f"{label} does not allocate an integer stream count")
        return 0
    return int(exact)


def verify_f5_domain_preflight(
    workload_path: str | Path,
    fixture_result_paths: list[str | Path],
    srt_state_paths: list[str | Path],
    dash_state_paths: list[str | Path],
    worker_result_paths: list[str | Path],
) -> dict:
    errors: list[str] = []
    workload, workload_sha256 = _read_object(workload_path, "workload")
    if workload.get("schemaVersion") != WORKLOAD_SCHEMA:
        errors.append(f"workload.schemaVersion must be {WORKLOAD_SCHEMA}")
    if workload.get("targetStreams") != TARGET_STREAMS:
        errors.append(f"workload.targetStreams must be {TARGET_STREAMS}")
    headroom = workload.get("headroomPercent")
    if (
        isinstance(headroom, bool)
        or not isinstance(headroom, (int, float))
        or not math.isfinite(headroom)
        or headroom < MINIMUM_HEADROOM_PERCENT
    ):
        errors.append(
            f"workload.headroomPercent must be at least {MINIMUM_HEADROOM_PERCENT}"
        )
    if workload.get("failureDomainsUnavailable") != 1:
        errors.append("workload.failureDomainsUnavailable must be 1")
    duration = workload.get("durationSeconds")
    if (
        isinstance(duration, bool)
        or not isinstance(duration, int)
        or duration < MINIMUM_DURATION_SECONDS
    ):
        errors.append(
            f"workload.durationSeconds must be at least {MINIMUM_DURATION_SECONDS}"
        )

    load_streams = workload.get("loadStreams")
    shape = workload.get("workerShape")
    placement = workload.get("placement")
    protocol_mix = workload.get("protocolMix")
    endpoint_distribution = workload.get("endpointDistribution")
    if isinstance(load_streams, bool) or not isinstance(load_streams, int) or load_streams < 1:
        errors.append("workload.loadStreams must be a positive integer")
        load_streams = 0
    elif load_streams < math.ceil(TARGET_STREAMS * (1 + MINIMUM_HEADROOM_PERCENT / 100)):
        errors.append("workload.loadStreams is below the F5 headroom requirement")
    if not isinstance(shape, dict):
        errors.append("workload.workerShape must be an object")
        shape = {}
    if not isinstance(placement, dict):
        errors.append("workload.placement must be an object")
        placement = {}
    if not isinstance(protocol_mix, dict):
        errors.append("workload.protocolMix must be an object")
        protocol_mix = {}
    if not isinstance(endpoint_distribution, dict):
        errors.append("workload.endpointDistribution must be an object")
        endpoint_distribution = {}

    zones = placement.get("zones")
    domain_counts = shape.get("failureDomainWorkerCounts")
    failure_domains = shape.get("failureDomains")
    worker_count = shape.get("count")
    if not isinstance(zones, list) or not zones or not all(
        isinstance(zone, str) and zone for zone in zones
    ) or len(zones) != len(set(zones)):
        errors.append("workload.placement.zones must be unique non-empty strings")
        zones = []
    if (
        not isinstance(failure_domains, int)
        or isinstance(failure_domains, bool)
        or failure_domains < 1
        or failure_domains != len(zones)
    ):
        errors.append("workload.workerShape.failureDomains must match placement zones")
        failure_domains = len(zones)
    if not isinstance(domain_counts, list) or len(domain_counts) != failure_domains or not all(
        isinstance(count, int) and not isinstance(count, bool) and count > 0
        for count in domain_counts
    ):
        errors.append("workload failure-domain worker counts are invalid")
        domain_counts = []
    if (
        isinstance(worker_count, bool)
        or not isinstance(worker_count, int)
        or worker_count < 1
        or not domain_counts
        or sum(domain_counts) != worker_count
    ):
        errors.append("workload worker count must equal its failure-domain counts")
        worker_count = 0

    inputs = {
        "fixture results": fixture_result_paths,
        "SRT states": srt_state_paths,
        "DASH states": dash_state_paths,
        "worker results": worker_result_paths,
    }
    for label, paths in inputs.items():
        if len(paths) != failure_domains:
            errors.append(f"{label} must contain exactly {failure_domains} files")

    input_sha256 = {
        "workload": workload_sha256,
        "fixtureResults": [],
        "srtStates": [],
        "dashStates": [],
        "workerResults": [],
    }
    fixture_projects = set()
    advertised_hosts = []
    image_digests = set()
    endpoints = set()
    protocol_counts = Counter()
    behavior_counts = Counter()
    started_at = []
    ended_at = []

    if all(len(paths) == failure_domains for paths in inputs.values()):
        for index, (result_path, srt_path, dash_path) in enumerate(
            zip(fixture_result_paths, srt_state_paths, dash_state_paths), start=1
        ):
            label = f"fixture domain {index}"
            result, result_sha256 = _read_object(result_path, f"{label} result")
            input_sha256["fixtureResults"].append(result_sha256)
            window = _validate_window(result, f"{label} result", errors)
            if window:
                started_at.append(window[0])
                ended_at.append(window[1])
            if result.get("passed") is not True or result.get("errors") != []:
                errors.append(f"{label} result did not pass cleanly")
            if result.get("immutableImage") is not True:
                errors.append(f"{label} did not report an immutable image")
            digest = _image_digest(result.get("fixtureImage"), f"{label}.fixtureImage", errors)
            if digest:
                image_digests.add(digest)
            checks = result.get("checks")
            if not isinstance(checks, list) or not FIXTURE_CHECKS.issubset(checks):
                errors.append(f"{label} is missing required startup checks")
            project = result.get("project")
            if not isinstance(project, str) or not project:
                errors.append(f"{label}.project is required")
            elif project in fixture_projects:
                errors.append(f"fixture domain project is duplicated: {project}")
            else:
                fixture_projects.add(project)

            inventory = result.get("fixtureInventory")
            if not isinstance(inventory, dict):
                errors.append(f"{label}.fixtureInventory must be an object")
                inventory = {}
            domain_hosts = set()
            for protocol, state_path in (("srt", srt_path), ("dash", dash_path)):
                try:
                    fixtures, state_sha256 = load_protocol_fixture_state(
                        state_path, protocol
                    )
                    raw_state_sha256 = hashlib.sha256(Path(state_path).read_bytes()).hexdigest()
                except (OSError, ValueError) as exc:
                    errors.append(f"{label} {protocol} state is invalid: {exc}")
                    continue
                input_sha256[f"{protocol}States"].append(raw_state_sha256)
                streams = [stream for behavior in BEHAVIORS for stream in fixtures[behavior]]
                state_endpoints = {stream["endpoint"] for stream in streams}
                hosts = {urlsplit(endpoint).hostname for endpoint in state_endpoints}
                if None in hosts or len(hosts) != 1:
                    errors.append(f"{label} {protocol} state must advertise one host")
                else:
                    domain_hosts.update(hosts)
                overlap = {(protocol, endpoint) for endpoint in state_endpoints} & endpoints
                if overlap:
                    errors.append(f"fixture endpoint is duplicated: {min(overlap)[1]}")
                endpoints.update((protocol, endpoint) for endpoint in state_endpoints)
                counts = {behavior: len(fixtures[behavior]) for behavior in BEHAVIORS}
                protocol_counts[protocol] += len(streams)
                behavior_counts.update(counts)
                recorded = inventory.get(protocol)
                if not isinstance(recorded, dict):
                    errors.append(f"{label} inventory is missing {protocol}")
                elif (
                    recorded.get("stateSha256") != state_sha256
                    or recorded.get("streamCount") != len(streams)
                    or recorded.get("distinctEndpointCount") != len(state_endpoints)
                    or recorded.get("behaviorEndpointCounts") != counts
                ):
                    errors.append(f"{label} {protocol} inventory does not match its state")
            if len(domain_hosts) != 1:
                errors.append(f"{label} SRT and DASH states must advertise the same host")
            else:
                advertised_hosts.extend(domain_hosts)

    expected_protocol_counts = {
        protocol: _expected_count(
            load_streams,
            protocol_mix.get(f"{protocol}Percent"),
            f"workload.protocolMix.{protocol}Percent",
            errors,
        )
        for protocol in PROTOCOLS
    }
    expected_behavior_counts = {
        behavior: _expected_count(
            load_streams,
            endpoint_distribution.get(f"{behavior}Percent"),
            f"workload.endpointDistribution.{behavior}Percent",
            errors,
        )
        for behavior in BEHAVIORS
    }
    if dict(protocol_counts) != expected_protocol_counts:
        errors.append(
            f"fixture protocol counts are {dict(protocol_counts)}, expected {expected_protocol_counts}"
        )
    if dict(behavior_counts) != expected_behavior_counts:
        errors.append(
            f"fixture behavior counts are {dict(behavior_counts)}, expected {expected_behavior_counts}"
        )
    if len(set(advertised_hosts)) != failure_domains:
        errors.append("fixture domains must use distinct advertised hosts")
    if len(endpoints) != load_streams:
        errors.append(f"fixture domains provide {len(endpoints)} distinct endpoints, expected {load_streams}")

    worker_domains = set()
    worker_ids = set()
    resolved_image_ids = set()
    if len(worker_result_paths) == failure_domains:
        for index, result_path in enumerate(worker_result_paths, start=1):
            label = f"worker domain {index}"
            result, result_sha256 = _read_object(result_path, f"{label} result")
            input_sha256["workerResults"].append(result_sha256)
            window = _validate_window(result, f"{label} result", errors)
            if window:
                started_at.append(window[0])
                ended_at.append(window[1])
            if result.get("passed") is not True or result.get("errors") != []:
                errors.append(f"{label} result did not pass cleanly")
            if result.get("immutableImage") is not True:
                errors.append(f"{label} did not report an immutable image")
            digest = _image_digest(result.get("workerImage"), f"{label}.workerImage", errors)
            if digest:
                image_digests.add(digest)
            checks = result.get("checks")
            if not isinstance(checks, list) or not WORKER_CHECKS.issubset(checks):
                errors.append(f"{label} is missing required startup checks")
            domain = result.get("failureDomain")
            if domain not in zones:
                errors.append(f"{label}.failureDomain is not in the workload")
                continue
            if domain in worker_domains:
                errors.append(f"worker failure domain is duplicated: {domain}")
                continue
            worker_domains.add(domain)
            expected_ids = [
                f"{domain}-worker-{number:02d}"
                for number in range(1, domain_counts[zones.index(domain)] + 1)
            ] if domain_counts else []
            actual_ids = result.get("expectedWorkerIds")
            if actual_ids != expected_ids:
                errors.append(f"{label} worker IDs do not match {domain}")
            elif worker_ids.intersection(actual_ids):
                errors.append(f"{label} contains duplicate worker IDs")
            else:
                worker_ids.update(actual_ids)
            resolved = result.get("resolvedImageId")
            if not isinstance(resolved, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", resolved):
                errors.append(f"{label}.resolvedImageId must be a SHA-256 image ID")
            else:
                resolved_image_ids.add(resolved)

    if worker_domains != set(zones):
        errors.append("worker results do not cover every workload failure domain")
    if len(worker_ids) != worker_count:
        errors.append(f"worker results contain {len(worker_ids)} unique IDs, expected {worker_count}")
    if len(image_digests) != 1:
        errors.append("fixture and worker domains must use one immutable image digest")
    if len(resolved_image_ids) != 1:
        errors.append("worker domains must resolve one image ID")

    return {
        "schemaVersion": REPORT_SCHEMA,
        "passed": not errors,
        "checks": (
            [
                "candidate_workload_shape_validated",
                "fixture_domain_artifacts_validated",
                "worker_domain_artifacts_validated",
                "cross_domain_identity_validated",
            ]
            if not errors
            else []
        ),
        "errors": errors,
        "targetStreams": workload.get("targetStreams"),
        "loadStreams": load_streams,
        "fixtureDomains": len(fixture_projects),
        "workerDomains": len(worker_domains),
        "workerCount": len(worker_ids),
        "protocolCounts": {protocol: protocol_counts[protocol] for protocol in PROTOCOLS},
        "behaviorCounts": {behavior: behavior_counts[behavior] for behavior in BEHAVIORS},
        "advertisedHosts": sorted(set(advertised_hosts)),
        "failureDomains": sorted(worker_domains),
        "imageDigest": next(iter(image_digests)) if len(image_digests) == 1 else None,
        "resolvedWorkerImageId": (
            next(iter(resolved_image_ids)) if len(resolved_image_ids) == 1 else None
        ),
        "startedAt": min(started_at).isoformat() if started_at else None,
        "endedAt": max(ended_at).isoformat() if ended_at else None,
        "inputSha256": input_sha256,
        "independentHostsCertified": False,
        "capacityCertified": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify collected F5 fixture and worker domain startup artifacts"
    )
    parser.add_argument("--workload", required=True)
    parser.add_argument("--fixture-result", action="append", required=True)
    parser.add_argument("--srt-state", action="append", required=True)
    parser.add_argument("--dash-state", action="append", required=True)
    parser.add_argument("--worker-result", action="append", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        report = verify_f5_domain_preflight(
            args.workload,
            args.fixture_result,
            args.srt_state,
            args.dash_state,
            args.worker_result,
        )
    except ValueError as exc:
        report = {
            "schemaVersion": REPORT_SCHEMA,
            "passed": False,
            "errors": [str(exc)],
            "independentHostsCertified": False,
            "capacityCertified": False,
        }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
