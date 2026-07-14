#!/usr/bin/env python3
"""Hard-stop one F5 worker domain and verify durable authority recovery."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from videosim.assignment_verifier import (
    capture_assignment_snapshot,
    verify_assignment_snapshot,
)
from videosim.distributed_benchmark import percentile
from videosim.host_identity import read_docker_host_identity, validate_host_identity
from videosim.postgres_store import PostgresControlPlaneStore


COMPOSE_FILE = ROOT / "docker-compose.worker-domain.yml"
REPORT_SCHEMA = "videosim.worker-domain-fault/v1"
P95_LIMIT_SECONDS = 45
P99_LIMIT_SECONDS = 90
CRITICAL_LOG_MARKERS = (
    "traceback (most recent call last)",
    "fatal:",
    "report_spool=blocked",
    "report spool quota exhausted",
    "certificate verify failed",
    "verified worker identity does not match workerid",
)


def _read_object(path: str | Path, label: str) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is invalid: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("snapshot timestamp is invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("snapshot timestamp must include a timezone")
    return parsed


def authoritative_owners(snapshot: dict) -> dict[str, str]:
    captured_at = _timestamp(snapshot.get("capturedAt"))
    feeds = {
        row["streamId"]: row
        for row in snapshot.get("feeds", [])
        if isinstance(row, dict) and row.get("desired") is True
    }
    workers = {
        row["workerId"]: row
        for row in snapshot.get("workers", [])
        if isinstance(row, dict)
        and row.get("state") == "active"
        and row.get("heartbeatFresh") is True
    }
    owners = {}
    for lease in snapshot.get("leases", []):
        if not isinstance(lease, dict):
            continue
        feed = feeds.get(lease.get("streamId"))
        worker = workers.get(lease.get("workerId"))
        if (
            feed is None
            or worker is None
            or lease.get("state") != "active"
            or lease.get("configVersion") != feed.get("configVersion")
            or lease.get("workerIncarnationId") != worker.get("incarnationId")
            or _timestamp(lease.get("expiresAt")) <= captured_at
        ):
            continue
        stream_id = lease["streamId"]
        if stream_id in owners:
            raise ValueError(f"duplicate authoritative lease for {stream_id}")
        owners[stream_id] = lease["workerId"]
    return owners


def update_recovery_times(
    baseline_owners: dict[str, str],
    current_owners: dict[str, str],
    affected: set[str],
    recovery_times: dict[str, float],
    elapsed: float,
) -> None:
    changed_healthy = {
        stream_id
        for stream_id, owner in baseline_owners.items()
        if stream_id not in affected and current_owners.get(stream_id) != owner
    }
    if changed_healthy:
        sample = ", ".join(sorted(changed_healthy)[:5])
        raise RuntimeError(f"healthy-domain authority changed: {sample}")
    for stream_id in affected:
        owner = current_owners.get(stream_id)
        if owner and owner != baseline_owners[stream_id]:
            recovery_times.setdefault(stream_id, elapsed)


def require_survivors_unchanged(
    baseline_snapshot: dict,
    current_snapshot: dict,
    failed_domain: str,
) -> None:
    def survivor_incarnations(snapshot: dict) -> dict[str, str]:
        return {
            worker["workerId"]: worker["incarnationId"]
            for worker in snapshot.get("workers", [])
            if isinstance(worker, dict)
            and worker.get("state") == "active"
            and worker.get("heartbeatFresh") is True
            and not str(worker.get("workerId", "")).startswith(
                f"{failed_domain}-worker-"
            )
        }

    baseline = survivor_incarnations(baseline_snapshot)
    current = survivor_incarnations(current_snapshot)
    if current != baseline:
        changed = sorted(set(baseline) ^ set(current))
        changed.extend(
            worker_id
            for worker_id in set(baseline) & set(current)
            if baseline[worker_id] != current[worker_id]
        )
        raise RuntimeError(
            "survivor worker incarnation changed: " + ", ".join(sorted(changed)[:5])
        )


def recovery_percentiles(values: list[float]) -> dict:
    ordered = sorted(values)
    return {
        name: round(percentile(ordered, quantile), 3)
        for name, quantile in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99))
    } | {"maximum": round(ordered[-1], 3)}


def validate_startup_result(result: dict, workload: dict) -> tuple[str, str, tuple[str, ...]]:
    if result.get("passed") is not True or result.get("errors") != []:
        raise ValueError("worker startup result did not pass cleanly")
    checks = result.get("checks")
    if not isinstance(checks, list) or not {
        "worker_registrations_validated",
        "worker_services_stable",
        "docker_logs_clean",
    }.issubset(checks):
        raise ValueError("worker startup result is missing required checks")
    project = result.get("project")
    domain = result.get("failureDomain")
    if not isinstance(project, str) or not project:
        raise ValueError("worker startup project is required")
    zones = workload.get("placement", {}).get("zones", [])
    counts = workload.get("workerShape", {}).get("failureDomainWorkerCounts", [])
    if domain not in zones or len(counts) != len(zones):
        raise ValueError("worker startup failure domain is not in the workload")
    expected_ids = tuple(
        f"{domain}-worker-{number:02d}"
        for number in range(1, counts[zones.index(domain)] + 1)
    )
    if result.get("expectedWorkerIds") != list(expected_ids):
        raise ValueError("worker startup IDs do not match the workload")
    registrations = result.get("workerRegistrations")
    if not isinstance(registrations, list) or len(registrations) != len(expected_ids):
        raise ValueError("worker startup registrations do not match the workload")
    registered_ids = set()
    incarnations = set()
    for item in registrations:
        if not isinstance(item, dict) or set(item) != {
            "workerId",
            "workerIncarnationId",
        }:
            raise ValueError("worker startup registration is invalid")
        worker_id = item["workerId"]
        if not isinstance(worker_id, str) or not worker_id:
            raise ValueError("worker startup registration worker ID is invalid")
        registered_ids.add(worker_id)
        raw_incarnation = item["workerIncarnationId"]
        try:
            incarnation = str(uuid.UUID(raw_incarnation))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("worker startup incarnation is invalid") from exc
        if incarnation != raw_incarnation or incarnation in incarnations:
            raise ValueError("worker startup incarnations must be canonical and unique")
        incarnations.add(incarnation)
    if registered_ids != set(expected_ids):
        raise ValueError("worker startup registrations do not match the workload")
    return project, domain, expected_ids


class WorkerDomainFault:
    def __init__(self, args):
        self.startup = _read_object(args.startup_result, "worker startup result")
        self.workload = _read_object(args.workload, "workload")
        self.input_sha256 = {
            "startupResult": hashlib.sha256(Path(args.startup_result).read_bytes()).hexdigest(),
            "workload": hashlib.sha256(Path(args.workload).read_bytes()).hexdigest(),
        }
        self.project, self.domain, self.worker_ids = validate_startup_result(
            self.startup, self.workload
        )
        self.services = {f"worker-{number:02d}" for number in range(1, len(self.worker_ids) + 1)}
        self.artifact_dir = Path(args.artifact_dir).resolve()
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.baseline_timeout = args.baseline_timeout
        self.recovery_timeout = args.recovery_timeout
        self.rejoin_timeout = args.rejoin_timeout
        self.rejoin_hold_seconds = args.rejoin_hold_seconds
        self.hold_seconds = args.hold_seconds
        self.poll_seconds = args.poll_seconds
        for name, value in (
            ("baseline_timeout", self.baseline_timeout),
            ("recovery_timeout", self.recovery_timeout),
            ("rejoin_timeout", self.rejoin_timeout),
            ("rejoin_hold_seconds", self.rejoin_hold_seconds),
            ("hold_seconds", self.hold_seconds),
            ("poll_seconds", self.poll_seconds),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive finite number")
        freshness = int(
            os.environ.get("VIDEOSIM_WORKER_FRESHNESS_SECONDS", "30")
        )
        self.store = PostgresControlPlaneStore(
            args.database_url,
            worker_freshness_seconds=freshness,
            min_pool_size=1,
            max_pool_size=2,
        )
        self.compose_command = [
            "docker",
            "compose",
            "-p",
            self.project,
            "-f",
            str(COMPOSE_FILE),
        ]

    def compose(self, *args: str, check: bool = True, **options):
        environment = os.environ | {"VIDEOSIM_FAILURE_DOMAIN": self.domain}
        return subprocess.run(
            [*self.compose_command, *args],
            cwd=ROOT,
            env=environment,
            check=check,
            timeout=options.pop("timeout", 600),
            **options,
        )

    def running_services(self) -> set[str]:
        result = self.compose(
            "ps", "--status", "running", "--services",
            text=True, capture_output=True, timeout=30,
        )
        return {line.strip() for line in result.stdout.splitlines() if line.strip()}

    def wait_for(self, description: str, condition, timeout: float):
        deadline = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < deadline:
            try:
                value = condition()
                if value:
                    return value
            except Exception as exc:
                last_error = exc
            time.sleep(self.poll_seconds)
        detail = f": {last_error}" if last_error else ""
        raise RuntimeError(f"timed out waiting for {description}{detail}")

    def snapshot_report(self, baseline: dict | None = None):
        snapshot = capture_assignment_snapshot(self.store)
        report = verify_assignment_snapshot(snapshot, self.workload, baseline)
        return snapshot, report

    def wait_for_baseline(self):
        def ready():
            snapshot, report = self.snapshot_report()
            if report.passed and report.metrics.get("phase") == "baseline":
                return snapshot, report
            raise RuntimeError("; ".join(report.errors))

        return self.wait_for(
            "assignment baseline", ready, timeout=self.baseline_timeout
        )

    def write_json(self, name: str, value: dict):
        (self.artifact_dir / name).write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def capture_compose(self, suffix: str):
        for name, args in {
            f"compose-ps-{suffix}.txt": ("ps", "--all"),
            f"docker-{suffix}.log": ("logs", "--no-color", "--timestamps"),
        }.items():
            with (self.artifact_dir / name).open("w", encoding="utf-8") as output:
                self.compose(
                    *args, check=False, stdout=output, stderr=subprocess.STDOUT, timeout=60
                )

    def run(self) -> dict:
        result = {
            "schemaVersion": REPORT_SCHEMA,
            "passed": False,
            "startedAt": datetime.now(timezone.utc).isoformat(),
            "project": self.project,
            "failureDomain": self.domain,
            "inputSha256": self.input_sha256,
            "checks": [],
            "errors": [],
            "independentHostsCertified": False,
            "mediaCapacityCertified": False,
            "capacityCertified": False,
        }
        stopped = False
        polls = []
        try:
            current_host = read_docker_host_identity()
            startup_host = validate_host_identity(self.startup.get("hostIdentity"))
            if current_host["dockerEngineId"] != startup_host["dockerEngineId"]:
                raise RuntimeError("worker fault must target the startup Docker engine")
            result["hostIdentity"] = current_host
            result["checks"].append("startup_host_identity_validated")

            if self.running_services() != self.services:
                raise RuntimeError("worker domain is not fully running before fault")
            baseline, baseline_report = self.wait_for_baseline()
            baseline_owners = authoritative_owners(baseline)
            affected = {
                stream_id
                for stream_id, owner in baseline_owners.items()
                if owner.startswith(f"{self.domain}-worker-")
            }
            if len(affected) != self.workload.get("loadStreams") // len(
                self.workload["placement"]["zones"]
            ):
                raise RuntimeError("baseline affected-stream count does not match the workload")
            self.write_json("assignment-baseline.json", baseline_report.payload())
            self.capture_compose("before")
            result["checks"].append("assignment_baseline_validated")

            self.compose("stop", "--timeout", "0", timeout=60)
            stopped = True
            self.wait_for(
                "worker domain stop", lambda: not self.running_services(), timeout=30
            )
            fault_started = time.monotonic()
            result["faultStartedAt"] = datetime.now(timezone.utc).isoformat()
            result["checks"].append("worker_domain_hard_stopped")
            recovery_times = {}
            recovered_snapshot = None
            recovered_report = None
            deadline = fault_started + self.recovery_timeout
            while time.monotonic() < deadline:
                snapshot, report = self.snapshot_report(baseline)
                elapsed = time.monotonic() - fault_started
                owners = authoritative_owners(snapshot)
                update_recovery_times(
                    baseline_owners, owners, affected, recovery_times, elapsed
                )
                polls.append(
                    {
                        "elapsedSeconds": round(elapsed, 3),
                        "freshWorkers": report.metrics.get("freshWorkers", 0),
                        "authoritativeAssignments": report.metrics.get(
                            "authoritativeAssignments", 0
                        ),
                        "affectedRecovered": len(recovery_times),
                    }
                )
                if (
                    report.passed
                    and report.metrics.get("phase") == "1-domain-loss"
                    and report.metrics.get("unavailableFailureDomains") == [self.domain]
                    and len(recovery_times) == len(affected)
                ):
                    recovered_snapshot, recovered_report = snapshot, report
                    break
                time.sleep(self.poll_seconds)
            if recovered_report is None:
                raise RuntimeError("worker-domain authority did not recover before timeout")
            timing = recovery_percentiles(list(recovery_times.values()))
            if timing["p95"] > P95_LIMIT_SECONDS:
                raise RuntimeError("worker-domain recovery exceeded the 45-second p95 limit")
            if timing["p99"] > P99_LIMIT_SECONDS:
                raise RuntimeError("worker-domain recovery exceeded the 90-second p99 limit")
            result["recovery"] = {
                "affectedStreams": len(affected),
                "recoveredStreams": len(recovery_times),
                "timingSeconds": timing,
                "p95LimitSeconds": P95_LIMIT_SECONDS,
                "p99LimitSeconds": P99_LIMIT_SECONDS,
            }
            self.write_json("assignment-domain-loss.json", recovered_report.payload())
            result["checks"].append("domain_loss_authority_recovered")

            time.sleep(self.hold_seconds)
            held_snapshot, held_report = self.snapshot_report(baseline)
            if not held_report.passed or authoritative_owners(held_snapshot) != authoritative_owners(
                recovered_snapshot
            ):
                raise RuntimeError("recovered authority did not remain stable")
            self.write_json("assignment-domain-loss-held.json", held_report.payload())
            self.capture_compose("fault")
            result["checks"].append("recovered_authority_stable")

            self.compose("start", timeout=120)
            self.wait_for(
                "worker domain restart",
                lambda: self.running_services() == self.services,
                timeout=60,
            )
            stopped = False

            def rejoined():
                snapshot, report = self.snapshot_report()
                if report.passed and report.metrics.get("phase") == "baseline":
                    return snapshot, report
                return None

            _, rejoined_report = self.wait_for(
                "worker domain assignment rejoin", rejoined, self.rejoin_timeout
            )
            self.write_json("assignment-rejoined.json", rejoined_report.payload())
            result["checks"].append("worker_domain_rejoined")

            hold_deadline = time.monotonic() + self.rejoin_hold_seconds
            while True:
                held_snapshot, held_report = self.snapshot_report()
                if not held_report.passed:
                    raise RuntimeError(
                        "rejoined assignment baseline changed: "
                        + "; ".join(held_report.errors)
                    )
                require_survivors_unchanged(
                    baseline,
                    held_snapshot,
                    self.domain,
                )
                if time.monotonic() >= hold_deadline:
                    break
                time.sleep(
                    min(self.poll_seconds, hold_deadline - time.monotonic())
                )
            self.write_json(
                "assignment-rejoined-held.json", held_report.payload()
            )
            self.capture_compose("rejoined")
            result["checks"].append("rejoined_survivors_stable")

            log_output = (self.artifact_dir / "docker-rejoined.log").read_text(
                encoding="utf-8"
            ).lower()
            if not log_output.strip():
                raise RuntimeError("worker Docker logs are empty")
            markers = [marker for marker in CRITICAL_LOG_MARKERS if marker in log_output]
            if markers:
                raise RuntimeError(
                    "worker Docker logs contain critical markers: " + ", ".join(markers)
                )
            result["checks"].append("docker_logs_clean")
            result["passed"] = True
        except Exception as exc:
            result["errors"].append(str(exc))
        finally:
            if stopped:
                try:
                    self.compose("start", check=False, timeout=120)
                except Exception as exc:
                    result["errors"].append(f"fail-safe worker restart failed: {exc}")
            try:
                self.capture_compose("final")
            except Exception as exc:
                result["errors"].append(f"artifact capture failed: {exc}")
            if polls:
                self.write_json("recovery-polls.json", {"polls": polls})
            try:
                self.store.close()
            except Exception as exc:
                result["errors"].append(f"database close failed: {exc}")
            if result["errors"]:
                result["passed"] = False
            result["endedAt"] = datetime.now(timezone.utc).isoformat()
            self.write_json("result.json", result)
        return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Hard-stop one F5 worker domain and verify authority recovery"
    )
    parser.add_argument("--startup-result", required=True)
    parser.add_argument(
        "--workload", default=str(ROOT / "scale/workloads/f5-1000-candidate.json")
    )
    parser.add_argument(
        "--database-url", default=os.environ.get("VIDEOSIM_DATABASE_URL", "")
    )
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--baseline-timeout", type=float, default=120)
    parser.add_argument("--recovery-timeout", type=float, default=120)
    parser.add_argument("--rejoin-timeout", type=float, default=300)
    parser.add_argument("--rejoin-hold-seconds", type=float, default=30)
    parser.add_argument("--hold-seconds", type=float, default=15)
    parser.add_argument("--poll-seconds", type=float, default=1)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.database_url:
        print("Worker domain fault FAIL: --database-url is required", file=sys.stderr)
        return 2
    try:
        workflow = WorkerDomainFault(args)
        result = workflow.run()
    except Exception as exc:
        print(f"Worker domain fault FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    print(f"Artifacts: {workflow.artifact_dir}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
