from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .postgres_store import DEFAULT_TENANT_ID, PostgresControlPlaneStore
from .scale_evidence import WORKLOAD_SCHEMA


SNAPSHOT_SCHEMA = "videosim.assignment-snapshot/v1"
REPORT_SCHEMA = "videosim.assignment-verification/v1"
PROTOCOL_CAPACITY_FIELDS = {"srt": "maxSrtStreams", "dash": "maxDashStreams"}


@dataclass(frozen=True)
class AssignmentVerificationReport:
    snapshot: dict
    metrics: dict
    errors: tuple[str, ...]
    workload_content_sha256: str
    baseline_snapshot_sha256: str = ""

    @property
    def passed(self) -> bool:
        return not self.errors

    def payload(self) -> dict:
        return {
            "schemaVersion": REPORT_SCHEMA,
            "passed": self.passed,
            "workloadContentSha256": self.workload_content_sha256,
            "snapshotSha256": _content_sha256(self.snapshot),
            "baselineSnapshotSha256": self.baseline_snapshot_sha256 or None,
            "metrics": self.metrics,
            "errors": list(self.errors),
            "snapshot": self.snapshot,
        }

    def to_json(self) -> str:
        return json.dumps(self.payload(), indent=2, sort_keys=True)


def capture_assignment_snapshot(store: PostgresControlPlaneStore) -> dict:
    """Capture feeds, workers, and leases at one PostgreSQL snapshot."""
    with store._pool.connection() as connection:
        with connection.transaction():
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            )
            captured_at = connection.execute(
                "SELECT transaction_timestamp() AS captured_at"
            ).fetchone()["captured_at"]
            feeds = connection.execute(
                """
                SELECT id, config ->> 'protocol' AS protocol, config_version,
                       config ->> 'source' = 'external'
                       OR (config ->> 'source' = 'generated'
                           AND config ->> 'desired_state' = 'running') AS desired
                FROM feeds
                WHERE tenant_id = %s
                ORDER BY id
                """,
                (store.tenant_id,),
            ).fetchall()
            workers = connection.execute(
                """
                SELECT worker_id, incarnation_id, state, last_heartbeat_at,
                       capacity,
                       state = 'active'
                       AND last_heartbeat_at > transaction_timestamp()
                           - (%s * interval '1 second') AS heartbeat_fresh
                FROM workers
                WHERE tenant_id = %s
                ORDER BY worker_id
                """,
                (store.worker_freshness_seconds, store.tenant_id),
            ).fetchall()
            leases = connection.execute(
                """
                SELECT stream_id, worker_id, worker_incarnation_id, epoch,
                       config_version, expires_at, state
                FROM leases
                WHERE tenant_id = %s
                ORDER BY stream_id
                """,
                (store.tenant_id,),
            ).fetchall()

    return {
        "schemaVersion": SNAPSHOT_SCHEMA,
        "capturedAt": _iso(captured_at),
        "tenantId": store.tenant_id,
        "workerFreshnessSeconds": store.worker_freshness_seconds,
        "feeds": [
            {
                "streamId": row["id"],
                "protocol": row["protocol"],
                "configVersion": int(row["config_version"]),
                "desired": bool(row["desired"]),
            }
            for row in feeds
        ],
        "workers": [
            {
                "workerId": row["worker_id"],
                "incarnationId": str(row["incarnation_id"]),
                "state": row["state"],
                "lastHeartbeatAt": _iso(row["last_heartbeat_at"]),
                "heartbeatFresh": bool(row["heartbeat_fresh"]),
                "capacity": dict(row["capacity"]),
            }
            for row in workers
        ],
        "leases": [
            {
                "streamId": row["stream_id"],
                "workerId": row["worker_id"],
                "workerIncarnationId": str(row["worker_incarnation_id"]),
                "epoch": int(row["epoch"]),
                "configVersion": int(row["config_version"]),
                "expiresAt": _iso(row["expires_at"]),
                "state": row["state"],
            }
            for row in leases
        ],
    }


def verify_assignment_snapshot(
    snapshot: dict, workload: dict, baseline_snapshot: dict | None = None
) -> AssignmentVerificationReport:
    errors: list[str] = []
    spec = _workload_spec(workload, errors)
    metrics: dict = {}
    baseline_sha256 = ""
    if spec is not None:
        metrics, owners, signatures, snapshot_errors = _analyze_snapshot(
            snapshot, spec, "snapshot"
        )
        errors.extend(snapshot_errors)
        if baseline_snapshot is not None:
            baseline_sha256 = _content_sha256(baseline_snapshot)
            baseline_metrics, baseline_owners, baseline_signatures, baseline_errors = (
                _analyze_snapshot(baseline_snapshot, spec, "baseline")
            )
            errors.extend(baseline_errors)
            if baseline_metrics.get("phase") != "baseline":
                errors.append("baseline snapshot must contain every declared failure domain")
            if signatures != baseline_signatures:
                errors.append("baseline and current desired stream catalogs differ")
            if not baseline_errors and signatures == baseline_signatures:
                unavailable = set(metrics.get("unavailableFailureDomains", []))
                changed = {
                    stream_id
                    for stream_id in set(owners) | set(baseline_owners)
                    if owners.get(stream_id) != baseline_owners.get(stream_id)
                }
                expected = {
                    stream_id
                    for stream_id, worker_id in baseline_owners.items()
                    if _worker_domain(worker_id, spec["zones"]) in unavailable
                }
                unexpected = changed - expected
                unchanged_failed = expected - changed
                metrics["ownershipChanges"] = len(changed)
                metrics["expectedOwnershipChanges"] = len(expected)
                if unexpected:
                    errors.append(
                        "ownership changed outside unavailable domains: "
                        + _id_summary(unexpected)
                    )
                if unchanged_failed:
                    errors.append(
                        "unavailable-domain ownership did not move: "
                        + _id_summary(unchanged_failed)
                    )

    return AssignmentVerificationReport(
        snapshot=snapshot,
        metrics=metrics,
        errors=tuple(errors),
        workload_content_sha256=_content_sha256(workload),
        baseline_snapshot_sha256=baseline_sha256,
    )


def run_assignment_verification(
    database_url: str,
    workload_path: str,
    *,
    baseline_path: str = "",
    output_path: str = "",
    tenant_id: str = DEFAULT_TENANT_ID,
) -> AssignmentVerificationReport:
    workload = _read_object(Path(workload_path), "workload")
    baseline = _read_baseline(Path(baseline_path)) if baseline_path else None
    store = PostgresControlPlaneStore(database_url, tenant_id=tenant_id)
    try:
        snapshot = capture_assignment_snapshot(store)
    finally:
        store.close()
    report = verify_assignment_snapshot(snapshot, workload, baseline)
    if output_path:
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp")
        temporary.write_text(report.to_json() + "\n", encoding="utf-8")
        os.replace(temporary, destination)
    return report


def human_summary(report: AssignmentVerificationReport) -> str:
    metrics = report.metrics
    status = "passed" if report.passed else "failed"
    summary = (
        f"Assignment verification {status}: phase={metrics.get('phase', 'invalid')} "
        f"desired={metrics.get('desiredStreams', 0)} "
        f"authoritative={metrics.get('authoritativeAssignments', 0)} "
        f"workers={metrics.get('freshWorkers', 0)}"
    )
    if not report.errors:
        return summary
    return summary + "\n" + "\n".join(f"- {error}" for error in report.errors)


def _analyze_snapshot(snapshot: dict, spec: dict, label: str):
    errors: list[str] = []
    parsed = _parse_snapshot(snapshot, label, errors)
    if parsed is None:
        return {}, {}, {}, errors
    captured_at, feeds, workers, leases = parsed
    desired = {stream_id: feed for stream_id, feed in feeds.items() if feed["desired"]}
    signatures = {
        stream_id: (feed["protocol"], feed["configVersion"])
        for stream_id, feed in desired.items()
    }

    if len(desired) != spec["loadStreams"]:
        errors.append(
            f"{label} desired stream count is {len(desired)}; expected {spec['loadStreams']}"
        )
    desired_protocols = {
        protocol: sum(feed["protocol"] == protocol for feed in desired.values())
        for protocol in PROTOCOL_CAPACITY_FIELDS
    }
    for protocol, expected in spec["protocolCounts"].items():
        if desired_protocols[protocol] != expected:
            errors.append(
                f"{label} desired {protocol.upper()} count is "
                f"{desired_protocols[protocol]}; expected {expected}"
            )

    fresh_workers = {
        worker_id: worker
        for worker_id, worker in workers.items()
        if worker["state"] == "active" and worker["heartbeatFresh"]
    }
    domain_workers = {zone: [] for zone in spec["zones"]}
    for worker_id, worker in fresh_workers.items():
        domain = _worker_domain(worker_id, spec["zones"])
        if domain is None:
            errors.append(f"{label} fresh worker is outside declared domains: {worker_id}")
            continue
        domain_workers[domain].append(worker_id)
        for field, expected in spec["capacity"].items():
            if worker["capacity"].get(field) != expected:
                errors.append(
                    f"{label} worker {worker_id} capacity.{field} is "
                    f"{worker['capacity'].get(field)!r}; expected {expected}"
                )
        pressure = worker["capacity"].get("pressure", {})
        if not isinstance(pressure, dict):
            errors.append(f"{label} worker {worker_id} capacity.pressure must be an object")
        elif pressure.get("spoolBlocked") is True:
            errors.append(f"{label} worker {worker_id} reports spoolBlocked=true")

    unavailable = []
    for zone, expected in zip(spec["zones"], spec["domainCounts"]):
        actual = len(domain_workers[zone])
        if actual == 0:
            unavailable.append(zone)
        elif actual != expected:
            errors.append(
                f"{label} failure domain {zone} has {actual} fresh workers; expected {expected} or 0"
            )
    if len(unavailable) not in (0, spec["failureDomainsUnavailable"]):
        errors.append(
            f"{label} has {len(unavailable)} unavailable failure domains; expected 0 or "
            f"{spec['failureDomainsUnavailable']}"
        )
    phase = "baseline" if not unavailable else f"{len(unavailable)}-domain-loss"

    authoritative: dict[str, list[dict]] = {}
    for lease in leases:
        feed = feeds.get(lease["streamId"])
        worker = workers.get(lease["workerId"])
        if (
            feed is not None
            and worker is not None
            and lease["state"] == "active"
            and lease["expiresAt"] > captured_at
            and lease["configVersion"] == feed["configVersion"]
            and worker["state"] == "active"
            and worker["heartbeatFresh"]
            and lease["workerIncarnationId"] == worker["incarnationId"]
        ):
            authoritative.setdefault(lease["streamId"], []).append(lease)

    missing = {stream_id for stream_id in desired if not authoritative.get(stream_id)}
    duplicate = {
        stream_id
        for stream_id, stream_leases in authoritative.items()
        if len(stream_leases) > 1
    }
    extra = set(authoritative) - set(desired)
    if missing:
        errors.append(f"{label} desired streams lack authority: {_id_summary(missing)}")
    if duplicate:
        errors.append(f"{label} streams have duplicate authority: {_id_summary(duplicate)}")
    if extra:
        errors.append(f"{label} undesired streams retain authority: {_id_summary(extra)}")

    owners = {
        stream_id: stream_leases[0]["workerId"]
        for stream_id, stream_leases in authoritative.items()
        if stream_id in desired and len(stream_leases) == 1
    }
    per_worker = {
        worker_id: {"total": 0, "srt": 0, "dash": 0}
        for worker_id in fresh_workers
    }
    for stream_id, worker_id in owners.items():
        if worker_id not in per_worker:
            continue
        protocol = desired[stream_id]["protocol"]
        per_worker[worker_id]["total"] += 1
        per_worker[worker_id][protocol] += 1

    if fresh_workers:
        _require_even_counts(
            [counts["total"] for counts in per_worker.values()],
            spec["loadStreams"],
            f"{label} total assignments",
            errors,
        )
        for protocol, expected in spec["protocolCounts"].items():
            _require_even_counts(
                [counts[protocol] for counts in per_worker.values()],
                expected,
                f"{label} {protocol.upper()} assignments",
                errors,
            )
    elif desired:
        errors.append(f"{label} has no fresh workers")

    for worker_id, counts in per_worker.items():
        capacity = fresh_workers[worker_id]["capacity"]
        for count_field, capacity_field in (
            ("total", "maxStreams"),
            ("srt", "maxSrtStreams"),
            ("dash", "maxDashStreams"),
        ):
            if counts[count_field] > capacity.get(capacity_field, 0):
                errors.append(
                    f"{label} worker {worker_id} exceeds {capacity_field}: "
                    f"{counts[count_field]} > {capacity.get(capacity_field, 0)}"
                )

    assigned_protocols = {
        protocol: sum(counts[protocol] for counts in per_worker.values())
        for protocol in PROTOCOL_CAPACITY_FIELDS
    }
    metrics = {
        "phase": phase,
        "tenantId": snapshot.get("tenantId", ""),
        "capturedAt": snapshot.get("capturedAt", ""),
        "desiredStreams": len(desired),
        "desiredByProtocol": desired_protocols,
        "freshWorkers": len(fresh_workers),
        "availableFailureDomains": [
            zone for zone in spec["zones"] if zone not in unavailable
        ],
        "unavailableFailureDomains": unavailable,
        "authoritativeAssignments": len(owners),
        "authoritativeByProtocol": assigned_protocols,
        "perWorker": [
            {
                "workerId": worker_id,
                "failureDomain": _worker_domain(worker_id, spec["zones"]),
                "assignedStreams": counts["total"],
                "srtStreams": counts["srt"],
                "dashStreams": counts["dash"],
            }
            for worker_id, counts in sorted(per_worker.items())
        ],
        "ownershipChanges": None,
        "expectedOwnershipChanges": None,
    }
    return metrics, owners, signatures, errors


def _parse_snapshot(snapshot: object, label: str, errors: list[str]):
    if not isinstance(snapshot, dict):
        errors.append(f"{label} must be a JSON object")
        return None
    if snapshot.get("schemaVersion") != SNAPSHOT_SCHEMA:
        errors.append(f"{label}.schemaVersion must be {SNAPSHOT_SCHEMA}")
    captured_at = _parse_time(snapshot.get("capturedAt"), f"{label}.capturedAt", errors)
    if not isinstance(snapshot.get("tenantId"), str) or not snapshot["tenantId"]:
        errors.append(f"{label}.tenantId must be a non-empty string")

    feeds = _unique_rows(snapshot.get("feeds"), "streamId", f"{label}.feeds", errors)
    workers = _unique_rows(snapshot.get("workers"), "workerId", f"{label}.workers", errors)
    lease_rows = snapshot.get("leases")
    if not isinstance(lease_rows, list) or not all(isinstance(row, dict) for row in lease_rows):
        errors.append(f"{label}.leases must be an array of objects")
        lease_rows = []

    parsed_feeds = {}
    for stream_id, feed in feeds.items():
        protocol = feed.get("protocol")
        if protocol not in PROTOCOL_CAPACITY_FIELDS:
            errors.append(f"{label} feed {stream_id} has unsupported protocol {protocol!r}")
            continue
        if not _positive_int(feed.get("configVersion")):
            errors.append(f"{label} feed {stream_id} configVersion must be positive")
            continue
        if not isinstance(feed.get("desired"), bool):
            errors.append(f"{label} feed {stream_id} desired must be boolean")
            continue
        parsed_feeds[stream_id] = feed

    parsed_workers = {}
    for worker_id, worker in workers.items():
        if not isinstance(worker.get("incarnationId"), str) or not worker["incarnationId"]:
            errors.append(f"{label} worker {worker_id} incarnationId is required")
            continue
        if not isinstance(worker.get("state"), str):
            errors.append(f"{label} worker {worker_id} state is required")
            continue
        if not isinstance(worker.get("heartbeatFresh"), bool):
            errors.append(f"{label} worker {worker_id} heartbeatFresh must be boolean")
            continue
        if not isinstance(worker.get("capacity"), dict):
            errors.append(f"{label} worker {worker_id} capacity must be an object")
            continue
        parsed_workers[worker_id] = worker

    parsed_leases = []
    for index, lease in enumerate(lease_rows):
        prefix = f"{label}.leases[{index}]"
        expires_at = _parse_time(lease.get("expiresAt"), f"{prefix}.expiresAt", errors)
        if not all(
            isinstance(lease.get(field), str) and lease[field]
            for field in ("streamId", "workerId", "workerIncarnationId", "state")
        ):
            errors.append(f"{prefix} string identifiers and state are required")
            continue
        if not _positive_int(lease.get("epoch")) or not _positive_int(
            lease.get("configVersion")
        ):
            errors.append(f"{prefix} epoch and configVersion must be positive")
            continue
        if expires_at is None:
            continue
        parsed_leases.append({**lease, "expiresAt": expires_at})

    if captured_at is None:
        return None
    return captured_at, parsed_feeds, parsed_workers, parsed_leases


def _workload_spec(workload: object, errors: list[str]) -> dict | None:
    if not isinstance(workload, dict):
        errors.append("workload must be a JSON object")
        return None
    if workload.get("schemaVersion") != WORKLOAD_SCHEMA:
        errors.append(f"workload.schemaVersion must be {WORKLOAD_SCHEMA}")
    load = workload.get("loadStreams")
    unavailable = workload.get("failureDomainsUnavailable")
    mix = workload.get("protocolMix")
    shape = workload.get("workerShape")
    placement = workload.get("placement")
    if not _positive_int(load):
        errors.append("workload.loadStreams must be a positive integer")
    if not _positive_int(unavailable):
        errors.append("workload.failureDomainsUnavailable must be a positive integer")
    if not isinstance(mix, dict):
        errors.append("workload.protocolMix must be an object")
    if not isinstance(shape, dict):
        errors.append("workload.workerShape must be an object")
    if not isinstance(placement, dict):
        errors.append("workload.placement must be an object")
    if errors:
        return None

    percentages = {protocol: mix.get(f"{protocol}Percent") for protocol in ("srt", "dash")}
    if not all(_non_negative_int(value) for value in percentages.values()) or sum(
        percentages.values()
    ) != 100:
        errors.append("workload.protocolMix must contain integer SRT/DASH percentages totaling 100")
    fields = (
        "count",
        "failureDomains",
        "maxStreams",
        "maxSrtStreams",
        "maxDashStreams",
    )
    if not all(_positive_int(shape.get(field)) for field in fields):
        errors.append("workload.workerShape assignment fields must be positive integers")
    zones = placement.get("zones")
    domain_counts = shape.get("failureDomainWorkerCounts")
    if not isinstance(zones, list) or not zones or not all(
        isinstance(zone, str) and zone for zone in zones
    ):
        errors.append("workload.placement.zones must be a non-empty string array")
    if not isinstance(domain_counts, list) or not domain_counts or not all(
        _positive_int(count) for count in domain_counts
    ):
        errors.append("workload.workerShape.failureDomainWorkerCounts must be positive integers")
    if errors:
        return None
    if len(set(zones)) != len(zones):
        errors.append("workload.placement.zones must be unique")
    if len(zones) != shape["failureDomains"] or len(domain_counts) != len(zones):
        errors.append("workload zones and failure-domain worker counts must match failureDomains")
    if sum(domain_counts) != shape["count"]:
        errors.append("workload failure-domain worker counts must total workerShape.count")
    if unavailable >= len(zones):
        errors.append("workload must retain at least one available failure domain")
    protocol_counts = {}
    for protocol, percentage in percentages.items():
        numerator = load * percentage
        if numerator % 100:
            errors.append(
                f"workload {protocol.upper()} percentage does not produce an exact stream count"
            )
        protocol_counts[protocol] = numerator // 100
    if errors:
        return None
    return {
        "loadStreams": load,
        "protocolCounts": protocol_counts,
        "failureDomainsUnavailable": unavailable,
        "zones": zones,
        "domainCounts": domain_counts,
        "capacity": {field: shape[field] for field in fields[2:]},
    }


def _unique_rows(value: object, key: str, label: str, errors: list[str]) -> dict:
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        errors.append(f"{label} must be an array of objects")
        return {}
    rows = {}
    for index, row in enumerate(value):
        identifier = row.get(key)
        if not isinstance(identifier, str) or not identifier:
            errors.append(f"{label}[{index}].{key} must be a non-empty string")
        elif identifier in rows:
            errors.append(f"{label} contains duplicate {key} {identifier}")
        else:
            rows[identifier] = row
    return rows


def _read_object(path: Path, label: str) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} could not be read as JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def _read_baseline(path: Path) -> dict:
    payload = _read_object(path, "baseline")
    if payload.get("schemaVersion") == REPORT_SCHEMA:
        snapshot = payload.get("snapshot")
        if not isinstance(snapshot, dict):
            raise ValueError("baseline verification report does not contain a snapshot")
        return snapshot
    return payload


def _parse_time(value: object, label: str, errors: list[str]) -> datetime | None:
    if not isinstance(value, str):
        errors.append(f"{label} must be an ISO-8601 timestamp")
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"{label} must be an ISO-8601 timestamp")
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        errors.append(f"{label} must include a timezone")
        return None
    return parsed


def _require_even_counts(
    actual: list[int], expected_total: int, label: str, errors: list[str]
):
    low, remainder = divmod(expected_total, len(actual))
    expected = [low] * (len(actual) - remainder) + [low + 1] * remainder
    if sorted(actual) != expected:
        errors.append(
            f"{label} are not evenly balanced: min={min(actual)}, max={max(actual)}, "
            f"total={sum(actual)}"
        )


def _worker_domain(worker_id: str, zones: list[str]) -> str | None:
    matches = [zone for zone in zones if worker_id.startswith(f"{zone}-worker-")]
    return matches[0] if len(matches) == 1 else None


def _id_summary(values: set[str]) -> str:
    ordered = sorted(values)
    sample = ", ".join(ordered[:5])
    suffix = ", ..." if len(ordered) > 5 else ""
    return f"{len(ordered)} ({sample}{suffix})"


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _non_negative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _content_sha256(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()
