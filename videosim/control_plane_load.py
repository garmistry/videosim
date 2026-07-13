from __future__ import annotations

import hashlib
import json
import math
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .assignment_verifier import _workload_spec
from .distributed_benchmark import percentile
from .postgres_store import CheckResult, FencedReport, PostgresControlPlaneStore
from .worker import worker_resource_snapshot


REPORT_SCHEMA = "videosim.control-plane-load/v1"


@dataclass(frozen=True)
class ControlPlaneLoadReport:
    tenant_id: str
    workload_content_sha256: str
    started_at: str
    ended_at: str
    requested_duration_seconds: float
    metrics: dict
    errors: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.errors

    def payload(self) -> dict:
        return {
            "schemaVersion": REPORT_SCHEMA,
            "scope": "postgresql_control_plane_only",
            "mediaProbesExecuted": False,
            "capacityCertified": False,
            "dataRetained": True,
            "passed": self.passed,
            "tenantId": self.tenant_id,
            "workloadContentSha256": self.workload_content_sha256,
            "startedAt": self.started_at,
            "endedAt": self.ended_at,
            "requestedDurationSeconds": self.requested_duration_seconds,
            "metrics": self.metrics,
            "errors": list(self.errors),
        }

    def to_json(self) -> str:
        return json.dumps(self.payload(), indent=2, sort_keys=True)


def parse_duration(value: str) -> float:
    units = {"s": 1, "m": 60, "h": 3600}
    text = str(value).strip().lower()
    try:
        seconds = float(text[:-1]) * units[text[-1]]
    except (KeyError, ValueError, IndexError) as exc:
        raise ValueError("duration must be a positive number ending in s, m, or h") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("duration must be greater than zero")
    return seconds


def run_control_plane_load(
    database_url: str,
    workload_path: str,
    duration_seconds: float,
    *,
    tick_seconds: float = 20,
    output_path: str = "",
) -> ControlPlaneLoadReport:
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise ValueError("duration_seconds must be finite and greater than zero")
    if not math.isfinite(tick_seconds) or tick_seconds <= 0:
        raise ValueError("tick_seconds must be finite and greater than zero")
    workload = _read_workload(Path(workload_path))
    spec = _load_spec(workload)
    tenant_id = f"control-plane-load-{uuid.uuid4()}"
    store = PostgresControlPlaneStore(database_url, tenant_id=tenant_id)
    started_at = datetime.now(timezone.utc)
    started = time.perf_counter()
    metrics = _empty_metrics(spec)
    errors: list[str] = []
    try:
        _assert_empty_database(store)
        workers, assignments, leases = _setup(store, tenant_id, spec)
        metrics["setupMs"] = round((time.perf_counter() - started) * 1000, 3)
        _run_load(
            store,
            workers,
            assignments,
            leases,
            spec,
            duration_seconds,
            tick_seconds,
            metrics,
            errors,
        )
        metrics.update(_database_metrics(store))
        _check_metrics(spec, metrics, errors)
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        store.close()

    resources = worker_resource_snapshot()
    metrics["processPeakRssBytes"] = resources["processPeakRssBytes"]
    if "openFileDescriptors" in resources:
        metrics["openFileDescriptors"] = resources["openFileDescriptors"]
    ended_at = datetime.now(timezone.utc)
    report = ControlPlaneLoadReport(
        tenant_id=tenant_id,
        workload_content_sha256=_sha256(workload),
        started_at=started_at.isoformat(),
        ended_at=ended_at.isoformat(),
        requested_duration_seconds=duration_seconds,
        metrics=metrics,
        errors=tuple(errors),
    )
    if output_path:
        _write_report(Path(output_path), report)
    return report


def human_summary(report: ControlPlaneLoadReport) -> str:
    metrics = report.metrics
    summary = (
        f"Control-plane load {'passed' if report.passed else 'failed'}: "
        f"streams={metrics.get('desiredStreams', 0)} "
        f"workers={metrics.get('freshWorkers', 0)} "
        f"ticks={metrics.get('ticks', 0)} "
        f"reports={metrics.get('reportsAccepted', 0)}"
    )
    return summary if not report.errors else summary + "\n" + "\n".join(
        f"- {error}" for error in report.errors
    )


def _read_workload(path: Path) -> dict:
    try:
        workload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"workload could not be read as JSON: {exc}") from exc
    if not isinstance(workload, dict):
        raise ValueError("workload must be a JSON object")
    return workload


def _load_spec(workload: dict) -> dict:
    spec = _workload_spec(workload)
    source_mix = workload.get("sourceMix")
    if not isinstance(source_mix, dict) or source_mix.get("externalPercent") != 100:
        raise ValueError("control-plane-load currently requires 100% external sources")
    profiles = workload.get("checkProfiles")
    if not isinstance(profiles, list) or not profiles:
        raise ValueError("workload.checkProfiles must be a non-empty array")
    parsed_profiles = []
    for index, profile in enumerate(profiles, 1):
        if not isinstance(profile, dict):
            raise ValueError("workload.checkProfiles must contain objects")
        cadence = profile.get("cadenceSeconds")
        if not isinstance(cadence, int) or isinstance(cadence, bool) or cadence <= 0:
            raise ValueError("workload check-profile cadenceSeconds must be positive")
        name = profile.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("workload check-profile name must be non-empty")
        parsed_profiles.append((f"probe.load.{index}", cadence, name))
    return spec | {"profiles": tuple(parsed_profiles)}


def _workers(spec: dict) -> list[tuple[str, uuid.UUID]]:
    return [
        (f"{zone}-worker-{number:02d}", uuid.uuid4())
        for zone, count in zip(spec["zones"], spec["domainCounts"])
        for number in range(1, count + 1)
    ]


def _feeds(spec: dict) -> list[tuple[str, str]]:
    return [
        (f"load-{protocol}-{number:06d}", protocol)
        for protocol, count in spec["protocols"].items()
        for number in range(count)
    ]


def _setup(store, tenant_id: str, spec: dict):
    workers = _workers(spec)
    feeds = _feeds(spec)
    assignments = {worker_id: [] for worker_id, _ in workers}
    for protocol in spec["protocols"]:
        protocol_feeds = [feed_id for feed_id, item_protocol in feeds if item_protocol == protocol]
        for index, feed_id in enumerate(protocol_feeds):
            assignments[workers[index % len(workers)][0]].append(feed_id)
    _check_assignment_capacity(assignments, feeds, spec)

    with store._pool.connection() as connection:
        with connection.transaction():
            connection.execute(
                "INSERT INTO tenants (id, name) VALUES (%s, %s)",
                (tenant_id, tenant_id),
            )
            connection.execute(
                """
                INSERT INTO feeds (tenant_id, id, config, config_version)
                SELECT %s, item.stream_id,
                       jsonb_build_object(
                           'id', item.stream_id, 'name', item.stream_id,
                           'source', 'external', 'protocol', item.protocol
                       ), 1
                FROM unnest(%s::text[], %s::text[]) AS item(stream_id, protocol)
                """,
                (tenant_id, [item[0] for item in feeds], [item[1] for item in feeds]),
            )

    capacity = spec["capacity"] | {"pressure": {"spoolBlocked": False}}
    leases = {}
    for worker_id, incarnation_id in workers:
        store.register_worker(
            worker_id,
            incarnation_id,
            f"CN={worker_id}",
            capacity=capacity,
            software_version="control-plane-load",
        )
        offered = store.reconcile_leases(
            assignments[worker_id],
            worker_id,
            incarnation_id,
            ttl_seconds=store.worker_freshness_seconds,
        )
        active = store.acknowledge_leases(
            [(lease.stream_id, lease.epoch, lease.config_version) for lease in offered],
            worker_id,
            incarnation_id,
            ttl_seconds=store.worker_freshness_seconds,
        )
        leases.update({lease.stream_id: lease for lease in active})
    return workers, assignments, leases


def _check_assignment_capacity(assignments, feeds, spec):
    protocols = dict(feeds)
    for worker_id, stream_ids in assignments.items():
        counts = {
            protocol: sum(protocols[stream_id] == protocol for stream_id in stream_ids)
            for protocol in spec["protocols"]
        }
        if len(stream_ids) > spec["capacity"]["maxStreams"]:
            raise ValueError(f"workload overfills {worker_id} total capacity")
        for protocol, count in counts.items():
            if count > spec["capacity"][f"max{protocol.title()}Streams"]:
                raise ValueError(f"workload overfills {worker_id} {protocol.upper()} capacity")


def _run_load(
    store,
    workers,
    assignments,
    leases,
    spec,
    duration_seconds,
    tick_seconds,
    metrics,
    errors,
):
    load_started = time.perf_counter()
    deadline = load_started + duration_seconds
    next_due = [load_started] * len(spec["profiles"])
    sequence = 0
    heartbeat_ms = []
    report_ms = []
    tick_ms = []
    while True:
        tick_started = time.perf_counter()
        for worker_id, incarnation_id in workers:
            operation_started = time.perf_counter()
            accepted = store.heartbeat(worker_id, incarnation_id)
            heartbeat_ms.append((time.perf_counter() - operation_started) * 1000)
            metrics["heartbeatsAccepted"] += int(accepted)
            if not accepted:
                errors.append(f"worker heartbeat was rejected: {worker_id}")
                break
        now = time.perf_counter()
        due = [index for index, due_at in enumerate(next_due) if now >= due_at]
        if due and not errors:
            sequence += 1
            metrics["reportWindows"] += 1
            metrics["profileWindows"] += len(due)
            for worker_id, incarnation_id in workers:
                observed_at = datetime.now(timezone.utc)
                results = tuple(
                    CheckResult(
                        uuid.uuid4(),
                        stream_id,
                        spec["profiles"][profile_index][0],
                        leases[stream_id].epoch,
                        leases[stream_id].config_version,
                        sequence,
                        "healthy",
                        observed_at,
                        {
                            "profile": spec["profiles"][profile_index][2],
                            "synthetic": True,
                        },
                    )
                    for stream_id in assignments[worker_id]
                    for profile_index in due
                )
                operation_started = time.perf_counter()
                disposition = store.ingest_report(
                    FencedReport(
                        uuid.uuid4(),
                        store.tenant_id,
                        worker_id,
                        incarnation_id,
                        results,
                    )
                )
                report_ms.append((time.perf_counter() - operation_started) * 1000)
                metrics["reportsAccepted"] += 1
                metrics["resultsAccepted"] += len(disposition.accepted_result_ids)
                metrics["duplicateResults"] += len(disposition.duplicate_result_ids)
                metrics["resultsRejected"] += len(disposition.rejected)
                if (
                    len(disposition.accepted_result_ids) != len(results)
                    or disposition.duplicate_result_ids
                    or disposition.rejected
                ):
                    errors.append(f"report disposition was incomplete: {worker_id}")
                    break
            for profile_index in due:
                cadence = spec["profiles"][profile_index][1]
                while next_due[profile_index] <= now:
                    next_due[profile_index] += cadence
        metrics["ticks"] += 1
        tick_ms.append((time.perf_counter() - tick_started) * 1000)
        if errors:
            break
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            break
        time.sleep(min(tick_seconds, remaining))
    metrics["loadElapsedSeconds"] = round(time.perf_counter() - load_started, 6)
    metrics["latencyMs"] = {
        "heartbeat": _percentiles(heartbeat_ms),
        "reportCommit": _percentiles(report_ms),
        "tick": _percentiles(tick_ms),
    }


def _database_metrics(store) -> dict:
    with store._pool.connection() as connection:
        row = connection.execute(
            """
            WITH desired AS (
                SELECT id FROM feeds WHERE tenant_id = %s
            ), authority AS (
                SELECT l.stream_id
                FROM leases l
                JOIN workers w
                  ON w.tenant_id = l.tenant_id AND w.worker_id = l.worker_id
                 AND w.incarnation_id = l.worker_incarnation_id
                WHERE l.tenant_id = %s AND l.state = 'active'
                  AND l.expires_at > transaction_timestamp()
                  AND w.state = 'active'
                  AND w.last_heartbeat_at > transaction_timestamp()
                      - (%s * interval '1 second')
            )
            SELECT
                (SELECT count(*) FROM desired) AS desired_streams,
                (SELECT count(*) FROM workers WHERE tenant_id = %s AND state = 'active'
                 AND last_heartbeat_at > transaction_timestamp()
                     - (%s * interval '1 second')) AS fresh_workers,
                (SELECT count(*) FROM authority) AS authoritative_leases,
                (SELECT count(DISTINCT stream_id) FROM current_check_state
                 WHERE tenant_id = %s) AS current_check_streams,
                (SELECT count(*) FROM worker_reports WHERE tenant_id = %s) AS worker_reports,
                (SELECT count(*) FROM check_results WHERE tenant_id = %s) AS check_results,
                (SELECT count(*) FROM current_check_state WHERE tenant_id = %s) AS current_checks,
                (SELECT count(*) FROM outbox
                 WHERE payload->>'tenantId' = %s) AS result_outbox_events,
                pg_database_size(current_database()) AS database_bytes
            """,
            (
                store.tenant_id,
                store.tenant_id,
                store.worker_freshness_seconds,
                store.tenant_id,
                store.worker_freshness_seconds,
                store.tenant_id,
                store.tenant_id,
                store.tenant_id,
                store.tenant_id,
                store.tenant_id,
            ),
        ).fetchone()
        worker_rows = connection.execute(
            """
            SELECT l.worker_id, count(*) AS total,
                   count(*) FILTER (WHERE f.config->>'protocol' = 'srt') AS srt,
                   count(*) FILTER (WHERE f.config->>'protocol' = 'dash') AS dash
            FROM leases l
            JOIN feeds f ON f.tenant_id = l.tenant_id AND f.id = l.stream_id
            WHERE l.tenant_id = %s AND l.state = 'active'
              AND l.expires_at > transaction_timestamp()
            GROUP BY l.worker_id
            ORDER BY l.worker_id
            """,
            (store.tenant_id,),
        ).fetchall()
    totals = [int(item["total"]) for item in worker_rows]
    srt = [int(item["srt"]) for item in worker_rows]
    dash = [int(item["dash"]) for item in worker_rows]
    return {
        "desiredStreams": int(row["desired_streams"]),
        "freshWorkers": int(row["fresh_workers"]),
        "authoritativeLeases": int(row["authoritative_leases"]),
        "currentCheckStreams": int(row["current_check_streams"]),
        "workerReports": int(row["worker_reports"]),
        "checkResults": int(row["check_results"]),
        "currentChecks": int(row["current_checks"]),
        "resultOutboxEvents": int(row["result_outbox_events"]),
        "databaseBytes": int(row["database_bytes"]),
        "assignmentShape": {
            "workers": len(worker_rows),
            "minimumStreams": min(totals, default=0),
            "maximumStreams": max(totals, default=0),
            "minimumSrtStreams": min(srt, default=0),
            "maximumSrtStreams": max(srt, default=0),
            "minimumDashStreams": min(dash, default=0),
            "maximumDashStreams": max(dash, default=0),
        },
    }


def _empty_metrics(spec: dict) -> dict:
    return {
        "expectedStreams": spec["load"],
        "expectedWorkers": sum(spec["domainCounts"]),
        "setupMs": 0,
        "loadElapsedSeconds": 0,
        "ticks": 0,
        "heartbeatsAccepted": 0,
        "reportWindows": 0,
        "profileWindows": 0,
        "reportsAccepted": 0,
        "resultsAccepted": 0,
        "duplicateResults": 0,
        "resultsRejected": 0,
        "latencyMs": {},
    }


def _check_metrics(spec: dict, metrics: dict, errors: list[str]):
    for field, expected in (
        ("desiredStreams", spec["load"]),
        ("freshWorkers", sum(spec["domainCounts"])),
        ("authoritativeLeases", spec["load"]),
        ("currentCheckStreams", spec["load"]),
    ):
        if metrics.get(field) != expected:
            errors.append(f"{field} is {metrics.get(field)}; expected {expected}")
    if metrics["ticks"] < 1 or metrics["reportsAccepted"] < 1:
        errors.append("load completed without a report cycle")
    if metrics["duplicateResults"] or metrics["resultsRejected"]:
        errors.append("load produced duplicate or rejected results")
    if metrics.get("resultOutboxEvents") != metrics["reportsAccepted"]:
        errors.append("accepted reports and retained result outbox events differ")


def _assert_empty_database(store):
    with store._pool.connection() as connection:
        row = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM tenants WHERE id <> 'default') AS extra_tenants,
                (SELECT count(*) FROM feeds) AS feeds,
                (SELECT count(*) FROM workers) AS workers,
                (SELECT count(*) FROM worker_reports) AS worker_reports,
                (SELECT count(*) FROM audit_events) AS audit_events,
                (SELECT count(*) FROM outbox) AS outbox_events,
                (SELECT count(*) FROM consumer_inbox) AS consumer_events
            """
        ).fetchone()
    occupied = {name: int(value) for name, value in row.items() if int(value)}
    if occupied:
        detail = ", ".join(f"{name}={count}" for name, count in occupied.items())
        raise ValueError(
            "control-plane-load requires an empty disposable database; " + detail
        )


def _percentiles(values: list[float]) -> dict:
    ordered = sorted(values)
    return {
        name: round(percentile(ordered, quantile), 3)
        for name, quantile in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99))
    }


def _sha256(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _write_report(path: Path, report: ControlPlaneLoadReport):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(report.to_json() + "\n", encoding="utf-8")
    os.replace(temporary, path)
