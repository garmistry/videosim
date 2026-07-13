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
PROTOCOLS = ("srt", "dash")


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
            "snapshotSha256": _sha256(self.snapshot),
            "baselineSnapshotSha256": self.baseline_snapshot_sha256 or None,
            "metrics": self.metrics,
            "errors": list(self.errors),
            "snapshot": self.snapshot,
        }

    def to_json(self) -> str:
        return json.dumps(self.payload(), indent=2, sort_keys=True)


def capture_assignment_snapshot(store: PostgresControlPlaneStore) -> dict:
    """Capture feeds, workers, and leases at one read-only PostgreSQL snapshot."""
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
                FROM feeds WHERE tenant_id = %s ORDER BY id
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
                FROM workers WHERE tenant_id = %s ORDER BY worker_id
                """,
                (store.worker_freshness_seconds, store.tenant_id),
            ).fetchall()
            leases = connection.execute(
                """
                SELECT stream_id, worker_id, worker_incarnation_id, epoch,
                       config_version, expires_at, state
                FROM leases WHERE tenant_id = %s ORDER BY stream_id
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
    metrics: dict = {}
    baseline_sha256 = ""
    try:
        spec = _workload_spec(workload)
        metrics, owners, signatures, current_errors = _analyze(snapshot, spec, "snapshot")
        errors.extend(current_errors)
    except (KeyError, TypeError, ValueError) as exc:
        errors.append(f"invalid assignment input: {exc}")
        owners, signatures = {}, {}

    if baseline_snapshot is not None and metrics:
        baseline_sha256 = _sha256(baseline_snapshot)
        try:
            baseline_metrics, baseline_owners, baseline_signatures, baseline_errors = (
                _analyze(baseline_snapshot, spec, "baseline")
            )
            errors.extend(baseline_errors)
            if baseline_metrics["phase"] != "baseline":
                errors.append("baseline snapshot must contain every declared failure domain")
            if signatures != baseline_signatures:
                errors.append("baseline and current desired stream catalogs differ")
            if not baseline_errors and signatures == baseline_signatures:
                unavailable = set(metrics["unavailableFailureDomains"])
                changed = {
                    stream_id
                    for stream_id in set(owners) | set(baseline_owners)
                    if owners.get(stream_id) != baseline_owners.get(stream_id)
                }
                expected = {
                    stream_id
                    for stream_id, worker_id in baseline_owners.items()
                    if _domain(worker_id, spec["zones"]) in unavailable
                }
                metrics["ownershipChanges"] = len(changed)
                metrics["expectedOwnershipChanges"] = len(expected)
                if changed - expected:
                    errors.append(
                        "ownership changed outside unavailable domains: "
                        + _ids(changed - expected)
                    )
                if expected - changed:
                    errors.append(
                        "unavailable-domain ownership did not move: "
                        + _ids(expected - changed)
                    )
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"invalid baseline assignment input: {exc}")

    return AssignmentVerificationReport(
        snapshot=snapshot,
        metrics=metrics,
        errors=tuple(errors),
        workload_content_sha256=_sha256(workload),
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
    _workload_spec(workload)
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
    summary = (
        f"Assignment verification {'passed' if report.passed else 'failed'}: "
        f"phase={metrics.get('phase', 'invalid')} "
        f"desired={metrics.get('desiredStreams', 0)} "
        f"authoritative={metrics.get('authoritativeAssignments', 0)} "
        f"workers={metrics.get('freshWorkers', 0)}"
    )
    return summary if not report.errors else summary + "\n" + "\n".join(
        f"- {error}" for error in report.errors
    )


def _analyze(snapshot: dict, spec: dict, label: str):
    if snapshot.get("schemaVersion") != SNAPSHOT_SCHEMA:
        raise ValueError(f"{label}.schemaVersion must be {SNAPSHOT_SCHEMA}")
    captured_at = _time(snapshot.get("capturedAt"), f"{label}.capturedAt")
    feeds = _index(snapshot.get("feeds"), "streamId", f"{label}.feeds")
    workers = _index(snapshot.get("workers"), "workerId", f"{label}.workers")
    leases = snapshot.get("leases")
    if not isinstance(leases, list) or not all(isinstance(row, dict) for row in leases):
        raise ValueError(f"{label}.leases must be an array of objects")

    for stream_id, feed in feeds.items():
        if feed.get("protocol") not in PROTOCOLS:
            raise ValueError(f"{label} feed {stream_id} has an invalid protocol")
        if not _positive(feed.get("configVersion")):
            raise ValueError(f"{label} feed {stream_id} has an invalid configVersion")
        if not isinstance(feed.get("desired"), bool):
            raise ValueError(f"{label} feed {stream_id} has an invalid desired flag")
    desired = {stream_id: feed for stream_id, feed in feeds.items() if feed["desired"]}
    signatures = {
        stream_id: (feed["protocol"], feed["configVersion"])
        for stream_id, feed in desired.items()
    }
    errors = []
    if len(desired) != spec["load"]:
        errors.append(f"{label} desired stream count is {len(desired)}; expected {spec['load']}")
    desired_protocols = {
        protocol: sum(feed["protocol"] == protocol for feed in desired.values())
        for protocol in PROTOCOLS
    }
    for protocol in PROTOCOLS:
        if desired_protocols[protocol] != spec["protocols"][protocol]:
            errors.append(
                f"{label} desired {protocol.upper()} count is {desired_protocols[protocol]}; "
                f"expected {spec['protocols'][protocol]}"
            )

    fresh = {}
    domain_workers = {zone: [] for zone in spec["zones"]}
    for worker_id, worker in workers.items():
        if not isinstance(worker.get("heartbeatFresh"), bool) or not isinstance(
            worker.get("capacity"), dict
        ):
            raise ValueError(f"{label} worker {worker_id} has invalid freshness/capacity")
        if worker.get("state") != "active" or not worker["heartbeatFresh"]:
            continue
        fresh[worker_id] = worker
        zone = _domain(worker_id, spec["zones"])
        if zone is None:
            errors.append(f"{label} fresh worker is outside declared domains: {worker_id}")
            continue
        domain_workers[zone].append(worker_id)
        for field, expected in spec["capacity"].items():
            if worker["capacity"].get(field) != expected:
                errors.append(
                    f"{label} worker {worker_id} capacity.{field} is "
                    f"{worker['capacity'].get(field)!r}; expected {expected}"
                )
        pressure = worker["capacity"].get("pressure", {})
        if not isinstance(pressure, dict) or pressure.get("spoolBlocked") is True:
            errors.append(f"{label} worker {worker_id} has invalid or blocked spool pressure")

    unavailable = []
    for zone, expected in zip(spec["zones"], spec["domainCounts"]):
        actual = len(domain_workers[zone])
        if actual == 0:
            unavailable.append(zone)
        elif actual != expected:
            errors.append(
                f"{label} failure domain {zone} has {actual} fresh workers; expected {expected} or 0"
            )
    if len(unavailable) not in (0, spec["unavailable"]):
        errors.append(
            f"{label} has {len(unavailable)} unavailable failure domains; "
            f"expected 0 or {spec['unavailable']}"
        )

    authoritative: dict[str, list[dict]] = {}
    for lease in leases:
        for field in ("streamId", "workerId", "workerIncarnationId", "state"):
            if not isinstance(lease.get(field), str) or not lease[field]:
                raise ValueError(f"{label} lease has invalid {field}")
        if not _positive(lease.get("epoch")) or not _positive(lease.get("configVersion")):
            raise ValueError(f"{label} lease has invalid epoch/configVersion")
        feed = feeds.get(lease["streamId"])
        worker = workers.get(lease["workerId"])
        if (
            feed
            and worker
            and lease["state"] == "active"
            and _time(lease.get("expiresAt"), f"{label} lease expiresAt") > captured_at
            and lease["configVersion"] == feed["configVersion"]
            and worker.get("state") == "active"
            and worker["heartbeatFresh"]
            and lease["workerIncarnationId"] == worker.get("incarnationId")
        ):
            authoritative.setdefault(lease["streamId"], []).append(lease)

    missing = {stream_id for stream_id in desired if not authoritative.get(stream_id)}
    duplicate = {stream_id for stream_id, rows in authoritative.items() if len(rows) > 1}
    extra = set(authoritative) - set(desired)
    for values, message in (
        (missing, "desired streams lack authority"),
        (duplicate, "streams have duplicate authority"),
        (extra, "undesired streams retain authority"),
    ):
        if values:
            errors.append(f"{label} {message}: {_ids(values)}")
    owners = {
        stream_id: rows[0]["workerId"]
        for stream_id, rows in authoritative.items()
        if stream_id in desired and len(rows) == 1
    }

    counts = {worker_id: {"total": 0, "srt": 0, "dash": 0} for worker_id in fresh}
    for stream_id, worker_id in owners.items():
        if worker_id in counts:
            counts[worker_id]["total"] += 1
            counts[worker_id][desired[stream_id]["protocol"]] += 1
    if counts:
        _balanced([row["total"] for row in counts.values()], spec["load"], f"{label} total", errors)
        for protocol in PROTOCOLS:
            _balanced(
                [row[protocol] for row in counts.values()],
                spec["protocols"][protocol],
                f"{label} {protocol.upper()}",
                errors,
            )
    elif desired:
        errors.append(f"{label} has no fresh workers")
    for worker_id, row in counts.items():
        for count, field in (
            (row["total"], "maxStreams"),
            (row["srt"], "maxSrtStreams"),
            (row["dash"], "maxDashStreams"),
        ):
            if count > fresh[worker_id]["capacity"].get(field, 0):
                errors.append(f"{label} worker {worker_id} exceeds {field}")

    metrics = {
        "phase": "baseline" if not unavailable else f"{len(unavailable)}-domain-loss",
        "tenantId": snapshot.get("tenantId", ""),
        "capturedAt": snapshot.get("capturedAt", ""),
        "desiredStreams": len(desired),
        "desiredByProtocol": desired_protocols,
        "freshWorkers": len(fresh),
        "availableFailureDomains": [zone for zone in spec["zones"] if zone not in unavailable],
        "unavailableFailureDomains": unavailable,
        "authoritativeAssignments": len(owners),
        "authoritativeByProtocol": {
            protocol: sum(row[protocol] for row in counts.values()) for protocol in PROTOCOLS
        },
        "perWorker": [
            {
                "workerId": worker_id,
                "failureDomain": _domain(worker_id, spec["zones"]),
                "assignedStreams": row["total"],
                "srtStreams": row["srt"],
                "dashStreams": row["dash"],
            }
            for worker_id, row in sorted(counts.items())
        ],
        "ownershipChanges": None,
        "expectedOwnershipChanges": None,
    }
    return metrics, owners, signatures, errors


def _workload_spec(workload: dict) -> dict:
    if not isinstance(workload, dict) or workload.get("schemaVersion") != WORKLOAD_SCHEMA:
        raise ValueError(f"workload.schemaVersion must be {WORKLOAD_SCHEMA}")
    load = _required_int(workload, "loadStreams")
    unavailable = _required_int(workload, "failureDomainsUnavailable")
    mix = workload.get("protocolMix")
    shape = workload.get("workerShape")
    placement = workload.get("placement")
    if not all(isinstance(value, dict) for value in (mix, shape, placement)):
        raise ValueError("workload protocolMix, workerShape, and placement are required")
    percentages = {protocol: mix.get(f"{protocol}Percent") for protocol in PROTOCOLS}
    if not all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in percentages.values()) or sum(percentages.values()) != 100:
        raise ValueError("workload SRT/DASH percentages must be integers totaling 100")
    protocols = {}
    for protocol, percentage in percentages.items():
        count, remainder = divmod(load * percentage, 100)
        if remainder:
            raise ValueError(f"workload {protocol.upper()} mix is not an exact stream count")
        protocols[protocol] = count
    capacity = {
        field: _required_int(shape, field)
        for field in ("maxStreams", "maxSrtStreams", "maxDashStreams")
    }
    worker_count = _required_int(shape, "count")
    failure_domains = _required_int(shape, "failureDomains")
    zones = placement.get("zones")
    domain_counts = shape.get("failureDomainWorkerCounts")
    if not isinstance(zones, list) or not zones or not all(isinstance(zone, str) and zone for zone in zones):
        raise ValueError("workload.placement.zones must be a non-empty string array")
    if len(set(zones)) != len(zones):
        raise ValueError("workload.placement.zones must be unique")
    if not isinstance(domain_counts, list) or not all(_positive(value) for value in domain_counts):
        raise ValueError("workload failure-domain worker counts must be positive integers")
    if len(zones) != failure_domains or len(domain_counts) != failure_domains:
        raise ValueError("workload zones/counts must match failureDomains")
    if sum(domain_counts) != worker_count or unavailable >= failure_domains:
        raise ValueError("workload failure-domain capacity is inconsistent")
    return {
        "load": load,
        "unavailable": unavailable,
        "protocols": protocols,
        "capacity": capacity,
        "zones": zones,
        "domainCounts": domain_counts,
    }


def _index(value: object, key: str, label: str) -> dict:
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ValueError(f"{label} must be an array of objects")
    rows = {}
    for row in value:
        identifier = row.get(key)
        if not isinstance(identifier, str) or not identifier or identifier in rows:
            raise ValueError(f"{label} contains an invalid or duplicate {key}")
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
        payload = payload.get("snapshot")
    if not isinstance(payload, dict):
        raise ValueError("baseline report does not contain a snapshot")
    return payload


def _balanced(actual: list[int], total: int, label: str, errors: list[str]):
    low, remainder = divmod(total, len(actual))
    expected = [low] * (len(actual) - remainder) + [low + 1] * remainder
    if sorted(actual) != expected:
        errors.append(
            f"{label} assignments are not balanced: min={min(actual)}, "
            f"max={max(actual)}, total={sum(actual)}"
        )


def _domain(worker_id: str, zones: list[str]) -> str | None:
    matches = [zone for zone in zones if worker_id.startswith(f"{zone}-worker-")]
    return matches[0] if len(matches) == 1 else None


def _time(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed


def _ids(values: set[str]) -> str:
    ordered = sorted(values)
    return f"{len(ordered)} ({', '.join(ordered[:5])}{', ...' if len(ordered) > 5 else ''})"


def _required_int(value: dict, field: str) -> int:
    result = value.get(field)
    if not _positive(result):
        raise ValueError(f"{field} must be a positive integer")
    return result


def _positive(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _sha256(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()
