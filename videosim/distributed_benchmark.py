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

from .gui import GuiState, apply_worker_report, register_worker, worker_assignments_payload


class BenchmarkInvariantError(RuntimeError):
    pass


@dataclass(frozen=True)
class ControlPlaneBenchmarkReport:
    stream_count: int
    worker_count: int
    iterations: int
    warmup_iterations: int
    seed: int
    cycle_durations_ms: tuple[float, ...]
    assignments_per_worker: dict[str, int]
    reports_accepted: int

    @property
    def passed(self) -> bool:
        return len(self.cycle_durations_ms) == self.iterations and self.reports_accepted == self.worker_count * self.iterations

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
            },
            "limitations": [
                "Exercises in-process assignment and report ingestion only.",
                "Does not open SRT/DASH endpoints or execute media probes.",
                "Does not establish production capacity, HA, security, or failover readiness.",
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
    *,
    monotonic: Callable[[], float] = time.perf_counter,
) -> ControlPlaneBenchmarkReport:
    for name, value, minimum in (
        ("stream_count", stream_count, 1),
        ("worker_count", worker_count, 1),
        ("iterations", iterations, 1),
        ("warmup_iterations", warmup_iterations, 0),
    ):
        if value < minimum:
            raise ValueError(f"{name} must be at least {minimum}")

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

        cycle_durations = []
        reports_accepted = 0
        assignments_per_worker: dict[str, int] = {}
        for cycle in range(warmup_iterations + iterations):
            order = list(worker_ids)
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
                    {"updatedAt": f"cycle-{cycle}", "alarms": [], "events": [], "pending": []},
                    assignment,
                )
                if not response.get("ok") or response.get("rejectedStreamIds"):
                    raise BenchmarkInvariantError(f"worker report was not fully accepted: {response}")
                if cycle >= warmup_iterations:
                    reports_accepted += 1
            elapsed_ms = max(0.0, (monotonic() - started) * 1000)
            if cycle >= warmup_iterations:
                cycle_durations.append(elapsed_ms)
                assignments_per_worker = {
                    worker_id: len(assignments[worker_id]["streams"]) for worker_id in worker_ids
                }

    return ControlPlaneBenchmarkReport(
        stream_count=stream_count,
        worker_count=worker_count,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        seed=seed,
        cycle_durations_ms=tuple(cycle_durations),
        assignments_per_worker=assignments_per_worker,
        reports_accepted=reports_accepted,
    )


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
