from __future__ import annotations

import json
import platform
import random
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .gui import (
    GuiState,
    WorkerReportConflict,
    apply_worker_report,
    drain_registered_worker,
    register_worker,
    worker_assignments_payload,
)


class BenchmarkInvariantError(RuntimeError):
    pass


@dataclass(frozen=True)
class ControlPlaneBenchmarkReport:
    stream_count: int
    worker_count: int
    iterations: int
    warmup_iterations: int
    seed: int
    failed_workers: tuple[str, ...]
    cycle_durations_ms: tuple[float, ...]
    assignments_per_worker: dict[str, int]
    reports_accepted: int
    stale_reports_rejected: int
    reassigned_streams: int
    minimum_reassignments: int
    excess_reassignments: int
    failover_duration_ms: float | None

    @property
    def passed(self) -> bool:
        survivors = self.worker_count - len(self.failed_workers)
        return (
            len(self.cycle_durations_ms) == self.iterations
            and self.reports_accepted == survivors * self.iterations
            and self.stale_reports_rejected == len(self.failed_workers)
        )

    def payload(self) -> dict:
        durations = sorted(self.cycle_durations_ms)
        return {
            "schemaVersion": 1,
            "scope": "in_process_control_plane_only",
            "mediaProbesExecuted": False,
            "capacityCertified": False,
            "passed": self.passed,
            "workload": {
                "streams": self.stream_count,
                "workers": self.worker_count,
                "iterations": self.iterations,
                "warmupIterations": self.warmup_iterations,
                "seed": self.seed,
                "failedWorkers": list(self.failed_workers),
            },
            "environment": {
                "python": sys.version.split()[0],
                "platform": platform.platform(),
            },
            "results": {
                "cycleDurationMs": {
                    "min": round(min(durations), 3),
                    "p50": round(percentile(durations, 0.50), 3),
                    "p95": round(percentile(durations, 0.95), 3),
                    "p99": round(percentile(durations, 0.99), 3),
                    "max": round(max(durations), 3),
                    "mean": round(statistics.fmean(durations), 3),
                },
                "assignmentsPerWorker": dict(sorted(self.assignments_per_worker.items())),
                "reportsAccepted": self.reports_accepted,
                "staleReportsRejected": self.stale_reports_rejected,
                "reassignedStreams": self.reassigned_streams,
                "minimumReassignments": self.minimum_reassignments,
                "excessReassignments": self.excess_reassignments,
                "failoverDurationMs": (
                    None
                    if self.failover_duration_ms is None
                    else round(self.failover_duration_ms, 3)
                ),
            },
            "limitations": [
                "Exercises in-process assignment and report ingestion only.",
                "Does not open SRT/DASH endpoints or execute media probes.",
                "Does not establish production capacity, HA, security, or failover readiness.",
                "Injected failure removes process-local workers; it does not exercise lease TTL, PostgreSQL, or failure-domain infrastructure.",
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.payload(), indent=2, sort_keys=True)


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    fraction = position - lower
    return values[lower] + (values[upper] - values[lower]) * fraction


def run_control_plane_benchmark(
    stream_count: int,
    worker_count: int,
    iterations: int,
    warmup_iterations: int,
    seed: int,
    failed_worker_count: int = 0,
    *,
    monotonic: Callable[[], float] = time.perf_counter,
) -> ControlPlaneBenchmarkReport:
    for name, value, minimum in (
        ("stream_count", stream_count, 1),
        ("worker_count", worker_count, 1),
        ("iterations", iterations, 1),
        ("warmup_iterations", warmup_iterations, 0),
        ("failed_worker_count", failed_worker_count, 0),
    ):
        if value < minimum:
            raise ValueError(f"{name} must be at least {minimum}")
    if failed_worker_count >= worker_count:
        raise ValueError("failed_worker_count must be less than worker_count")

    randomizer = random.Random(seed)
    with tempfile.TemporaryDirectory(prefix="videosim-control-plane-benchmark-") as directory:
        state = GuiState(
            monitor_state_path=str(Path(directory) / "monitor.json"),
            control_plane_instance_id=f"benchmark-seed-{seed}",
            allow_legacy_worker_reports=False,
        )
        for index in range(stream_count):
            state.create_stream(
                name=f"Benchmark stream {index + 1}",
                source="external",
                external_url=f"srt://benchmark-{index + 1}.invalid:{10000 + index}?mode=caller",
                select=False,
            )
        expected_stream_ids = set(state.streams)
        worker_ids = [f"worker-{index + 1}" for index in range(worker_count)]
        for worker_id in worker_ids:
            register_worker(state, worker_id)

        def execute_cycle(active_worker_ids: list[str], cycle_label: str):
            order = list(active_worker_ids)
            randomizer.shuffle(order)
            started = monotonic()
            assignments = {
                worker_id: worker_assignments_payload(state, worker_id, "http://benchmark.invalid")
                for worker_id in order
            }
            assert_assignment_invariants(assignments, expected_stream_ids)
            for worker_id in order:
                assignment = assignments[worker_id]
                stream_ids = [stream["id"] for stream in assignment["streams"]]
                response = apply_worker_report(
                    state,
                    worker_id,
                    stream_ids,
                    {"updatedAt": cycle_label, "alarms": [], "events": [], "pending": []},
                    assignment,
                )
                if not response.get("ok") or response.get("rejectedStreamIds"):
                    raise BenchmarkInvariantError(f"worker report was not fully accepted: {response}")
            elapsed_ms = max(0.0, (monotonic() - started) * 1000)
            return assignments, elapsed_ms

        for cycle in range(warmup_iterations):
            execute_cycle(worker_ids, f"warmup-{cycle}")

        failed_workers = tuple(worker_ids[:failed_worker_count])
        active_worker_ids = [
            worker_id for worker_id in worker_ids if worker_id not in failed_workers
        ]
        stale_reports_rejected = 0
        reassigned_streams = 0
        minimum_reassignments = 0
        excess_reassignments = 0
        failover_duration_ms = None
        if failed_workers:
            baseline = {
                worker_id: worker_assignments_payload(
                    state, worker_id, "http://benchmark.invalid"
                )
                for worker_id in worker_ids
            }
            assert_assignment_invariants(baseline, expected_stream_ids)
            baseline_owners = assignment_owners(baseline)
            minimum_reassignments = sum(
                owner in failed_workers for owner in baseline_owners.values()
            )
            for worker_id in failed_workers:
                drain_registered_worker(state, worker_id)
            failover_started = monotonic()
            failover = {
                worker_id: worker_assignments_payload(
                    state, worker_id, "http://benchmark.invalid"
                )
                for worker_id in active_worker_ids
            }
            assert_assignment_invariants(failover, expected_stream_ids)
            failover_duration_ms = max(0.0, (monotonic() - failover_started) * 1000)
            failover_owners = assignment_owners(failover)
            reassigned_streams = sum(
                baseline_owners[stream_id] != failover_owners[stream_id]
                for stream_id in expected_stream_ids
            )
            excess_reassignments = max(
                0, reassigned_streams - minimum_reassignments
            )
            for worker_id in failed_workers:
                stale = baseline[worker_id]
                try:
                    apply_worker_report(
                        state,
                        worker_id,
                        [stream["id"] for stream in stale["streams"]],
                        {"updatedAt": "stale", "alarms": [], "events": [], "pending": []},
                        stale,
                    )
                except WorkerReportConflict:
                    stale_reports_rejected += 1
                else:
                    raise BenchmarkInvariantError(
                        f"failed worker {worker_id} stale report was accepted"
                    )

        cycle_durations = []
        reports_accepted = 0
        assignments_per_worker: dict[str, int] = {}
        for cycle in range(iterations):
            assignments, elapsed_ms = execute_cycle(
                active_worker_ids, f"cycle-{cycle}"
            )
            reports_accepted += len(active_worker_ids)
            cycle_durations.append(elapsed_ms)
            assignments_per_worker = {
                worker_id: len(assignments[worker_id]["streams"])
                for worker_id in active_worker_ids
            }

    return ControlPlaneBenchmarkReport(
        stream_count=stream_count,
        worker_count=worker_count,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        seed=seed,
        failed_workers=failed_workers,
        cycle_durations_ms=tuple(cycle_durations),
        assignments_per_worker=assignments_per_worker,
        reports_accepted=reports_accepted,
        stale_reports_rejected=stale_reports_rejected,
        reassigned_streams=reassigned_streams,
        minimum_reassignments=minimum_reassignments,
        excess_reassignments=excess_reassignments,
        failover_duration_ms=failover_duration_ms,
    )


def assignment_owners(assignments: dict[str, dict]) -> dict[str, str]:
    return {
        stream["id"]: worker_id
        for worker_id, assignment in assignments.items()
        for stream in assignment.get("streams", [])
    }


def assert_assignment_invariants(assignments: dict[str, dict], expected_stream_ids: set[str]):
    owners: dict[str, str] = {}
    generations = set()
    instances = set()
    for worker_id, assignment in assignments.items():
        generations.add(assignment.get("assignmentGeneration"))
        instances.add(assignment.get("controlPlaneInstanceId"))
        if not assignment.get("assignmentToken"):
            raise BenchmarkInvariantError(f"{worker_id} assignment has no token")
        for stream in assignment.get("streams", []):
            stream_id = stream.get("id")
            if stream_id in owners:
                raise BenchmarkInvariantError(f"stream {stream_id} assigned to both {owners[stream_id]} and {worker_id}")
            owners[stream_id] = worker_id
    assigned = set(owners)
    if assigned != expected_stream_ids:
        missing = sorted(expected_stream_ids - assigned)
        unexpected = sorted(assigned - expected_stream_ids)
        raise BenchmarkInvariantError(f"assignment coverage mismatch: missing={missing} unexpected={unexpected}")
    if len(generations) != 1 or None in generations:
        raise BenchmarkInvariantError(f"workers observed inconsistent assignment generations: {generations}")
    if len(instances) != 1 or None in instances:
        raise BenchmarkInvariantError(f"workers observed inconsistent control-plane instances: {instances}")


def human_summary(report: ControlPlaneBenchmarkReport) -> str:
    payload = report.payload()
    durations = payload["results"]["cycleDurationMs"]
    return "\n".join(
        [
            "Control-plane benchmark: PASS" if report.passed else "Control-plane benchmark: FAIL",
            "Scope: in-process assignment/report path only; no media probes or capacity certification",
            f"Streams: {report.stream_count}",
            f"Workers: {report.worker_count}",
            f"Measured iterations: {report.iterations}",
            f"Cycle p50/p95/p99: {durations['p50']}/{durations['p95']}/{durations['p99']} ms",
        ]
    )
