from __future__ import annotations

import hashlib
import json
import platform
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .control_plane import MAX_REPORT_STREAMS
from .distributed_benchmark import percentile
from .monitor import empty_monitor_state, run_monitor_once
from .worker import worker_resource_pressure, worker_resource_snapshot


@dataclass(frozen=True)
class WorkerBenchmarkReport:
    scenario: str
    scenario_sha256: str
    stream_count: int
    iterations: int
    warmup_iterations: int
    cycle_durations_ms: tuple[float, ...]
    batch_cpu_ms: tuple[float, ...]
    stream_counts: tuple[int, ...]
    check_counts: tuple[int, ...]
    outcomes: dict[str, int]
    require_full_validation_coverage: bool
    max_validation_gap_cycles: int
    validation_attempted_streams: int
    cycles_to_full_validation_coverage: int | None
    minimum_validation_attempts: int
    maximum_validation_gap_cycles: int | None
    process_peak_rss_bytes: int
    child_peak_rss_bytes: int
    open_file_descriptors: int | None

    @property
    def passed(self) -> bool:
        return (
            len(self.cycle_durations_ms) == self.iterations
            and all(count == self.stream_count for count in self.stream_counts)
            and (
                not self.require_full_validation_coverage
                or self.validation_attempted_streams == self.stream_count
            )
            and (
                self.max_validation_gap_cycles == 0
                or (
                    self.minimum_validation_attempts >= 2
                    and self.maximum_validation_gap_cycles is not None
                    and self.maximum_validation_gap_cycles
                    <= self.max_validation_gap_cycles
                )
            )
        )

    @property
    def validation_coverage_percent(self) -> float:
        return round(self.validation_attempted_streams * 100 / self.stream_count, 3)

    def payload(self) -> dict:
        return {
            "schemaVersion": "videosim.worker-benchmark/v1",
            "scope": "single_worker_real_media_probes",
            "mediaProbesExecuted": True,
            "capacityCertified": False,
            "passed": self.passed,
            "scenario": {
                "path": self.scenario,
                "sha256": self.scenario_sha256,
                "streams": self.stream_count,
            },
            "workload": {
                "iterations": self.iterations,
                "warmupIterations": self.warmup_iterations,
                "requireFullValidationCoverage": self.require_full_validation_coverage,
                "maxValidationGapCycles": self.max_validation_gap_cycles,
            },
            "environment": {
                "python": sys.version.split()[0],
                "platform": platform.platform(),
            },
            "results": {
                "cycleDurationMs": summary(self.cycle_durations_ms),
                "batchCpuMs": summary(self.batch_cpu_ms),
                "streamCounts": list(self.stream_counts),
                "checkCounts": list(self.check_counts),
                "outcomes": dict(sorted(self.outcomes.items())),
                "validationAttemptedStreams": self.validation_attempted_streams,
                "validationCoveragePercent": self.validation_coverage_percent,
                "cyclesToFullValidationCoverage": self.cycles_to_full_validation_coverage,
                "minimumValidationAttempts": self.minimum_validation_attempts,
                "maximumValidationGapCycles": self.maximum_validation_gap_cycles,
                "processPeakRssBytes": self.process_peak_rss_bytes,
                "childPeakRssBytes": self.child_peak_rss_bytes,
                "openFileDescriptors": self.open_file_descriptors,
            },
            "limitations": [
                "Measures one worker against the supplied live endpoints.",
                "Validation gap cycles are scheduler cadence, not an approved wall-time freshness SLO.",
                "Peak RSS values are cumulative single-process peaks, not aggregate concurrent child RSS.",
                "Does not certify distributed capacity, headroom, HA, failure recovery, or soak duration.",
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.payload(), indent=2, sort_keys=True)


def summary(values: tuple[float, ...]) -> dict:
    ordered = sorted(values)
    return {
        "min": round(ordered[0], 3),
        "p50": round(percentile(ordered, 0.50), 3),
        "p95": round(percentile(ordered, 0.95), 3),
        "p99": round(percentile(ordered, 0.99), 3),
        "max": round(ordered[-1], 3),
        "mean": round(statistics.fmean(ordered), 3),
    }


def load_scenario(path: str | Path) -> tuple[dict, str, int]:
    scenario_path = Path(path)
    try:
        raw = scenario_path.read_bytes()
        scenario = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"worker benchmark scenario is invalid: {exc}") from exc
    if not isinstance(scenario, dict) or not isinstance(scenario.get("streams"), list):
        raise ValueError("worker benchmark scenario must contain a streams array")
    streams = scenario["streams"]
    if not 1 <= len(streams) <= MAX_REPORT_STREAMS:
        raise ValueError(
            f"worker benchmark scenario must contain 1-{MAX_REPORT_STREAMS} streams"
        )
    identifiers = []
    for index, stream in enumerate(streams):
        if not isinstance(stream, dict):
            raise ValueError(f"worker benchmark streams[{index}] must be an object")
        stream_id = stream.get("id")
        if not isinstance(stream_id, str) or not stream_id:
            raise ValueError(f"worker benchmark streams[{index}].id is required")
        identifiers.append(stream_id)
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("worker benchmark stream IDs must be unique")
    running = sum(stream.get("status") == "running" for stream in streams)
    if running == 0:
        raise ValueError("worker benchmark scenario has no running streams")
    return scenario, hashlib.sha256(raw).hexdigest(), running


def run_worker_benchmark(
    scenario_path: str | Path,
    iterations: int,
    warmup_iterations: int,
    srt_host: str,
    max_concurrent_checks: int,
    max_concurrent_deep_checks: int,
    stream_budget_seconds: float,
    deep_check_interval_seconds: float,
    batch_budget_seconds: float,
    require_full_validation_coverage: bool = False,
    max_validation_gap_cycles: int = 0,
    *,
    monitor: Callable = run_monitor_once,
    resource_snapshot: Callable[[], dict] = worker_resource_snapshot,
    monotonic: Callable[[], float] = time.perf_counter,
) -> WorkerBenchmarkReport:
    if iterations < 1 or warmup_iterations < 0:
        raise ValueError("iterations must be positive and warmup iterations non-negative")
    if max_validation_gap_cycles < 0:
        raise ValueError("max validation gap cycles must be non-negative")
    scenario, scenario_sha256, stream_count = load_scenario(scenario_path)
    state = empty_monitor_state()
    running_stream_ids = {
        stream["id"]
        for stream in scenario["streams"]
        if stream.get("status") == "running"
    }
    durations = []
    cpu = []
    stream_counts = []
    check_counts = []
    outcomes: dict[str, int] = {}
    process_peak = 0
    child_peak = 0
    open_fds = None
    validation_attempted_stream_ids = set()
    validation_attempt_counts = {stream_id: 0 for stream_id in running_stream_ids}
    last_validation_attempt_cycles = {}
    maximum_validation_gap_cycles = None
    cycles_to_full_validation_coverage = None
    for cycle in range(iterations + warmup_iterations):
        before = resource_snapshot()
        started = monotonic()
        state = monitor(
            scenario,
            state,
            time.time(),
            5,
            1000,
            srt_host,
            max_concurrency=max_concurrent_checks,
            max_deep_concurrency=max_concurrent_deep_checks,
            stream_budget_seconds=stream_budget_seconds,
            deep_check_interval_seconds=deep_check_interval_seconds,
            batch_budget_seconds=batch_budget_seconds,
        )
        elapsed_ms = max(0.0, (monotonic() - started) * 1000)
        resources = worker_resource_pressure(before, resource_snapshot())
        if cycle < warmup_iterations:
            continue
        metrics = state.get("probeMetrics")
        if not isinstance(metrics, dict):
            raise ValueError("worker benchmark monitor returned no probe metrics")
        durations.append(elapsed_ms)
        cpu.append(resources["lastBatchCpuMs"])
        stream_counts.append(int(metrics.get("streamCount", 0)))
        check_counts.append(int(metrics.get("checkCount", 0)))
        attempted_this_cycle = set()
        for item in metrics.get("streams", []):
            if (
                isinstance(item, dict)
                and item.get("check") == "validation"
                and item.get("outcome") != "skipped"
                and item.get("streamId") in running_stream_ids
            ):
                attempted_this_cycle.add(item["streamId"])
        measured_cycle = cycle - warmup_iterations + 1
        for stream_id in attempted_this_cycle:
            previous_cycle = last_validation_attempt_cycles.get(stream_id)
            gap = (
                measured_cycle
                if previous_cycle is None
                else measured_cycle - previous_cycle
            )
            maximum_validation_gap_cycles = max(
                maximum_validation_gap_cycles or 0, gap
            )
            last_validation_attempt_cycles[stream_id] = measured_cycle
            validation_attempt_counts[stream_id] += 1
        validation_attempted_stream_ids.update(attempted_this_cycle)
        if (
            cycles_to_full_validation_coverage is None
            and validation_attempted_stream_ids == running_stream_ids
        ):
            cycles_to_full_validation_coverage = cycle - warmup_iterations + 1
        for outcome, count in metrics.get("outcomes", {}).items():
            outcomes[str(outcome)] = outcomes.get(str(outcome), 0) + int(count)
        process_peak = max(process_peak, resources["processPeakRssBytes"])
        child_peak = max(child_peak, resources["childPeakRssBytes"])
        if "openFileDescriptors" in resources:
            open_fds = max(open_fds or 0, resources["openFileDescriptors"])
    for stream_id in running_stream_ids:
        last_cycle = last_validation_attempt_cycles.get(stream_id)
        trailing_gap = (
            iterations + 1 if last_cycle is None else iterations - last_cycle + 1
        )
        maximum_validation_gap_cycles = max(
            maximum_validation_gap_cycles or 0, trailing_gap
        )
    return WorkerBenchmarkReport(
        scenario=str(scenario_path),
        scenario_sha256=scenario_sha256,
        stream_count=stream_count,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        cycle_durations_ms=tuple(durations),
        batch_cpu_ms=tuple(cpu),
        stream_counts=tuple(stream_counts),
        check_counts=tuple(check_counts),
        outcomes=outcomes,
        require_full_validation_coverage=require_full_validation_coverage,
        max_validation_gap_cycles=max_validation_gap_cycles,
        validation_attempted_streams=len(validation_attempted_stream_ids),
        cycles_to_full_validation_coverage=cycles_to_full_validation_coverage,
        minimum_validation_attempts=min(validation_attempt_counts.values()),
        maximum_validation_gap_cycles=maximum_validation_gap_cycles,
        process_peak_rss_bytes=process_peak,
        child_peak_rss_bytes=child_peak,
        open_file_descriptors=open_fds,
    )


def human_summary(report: WorkerBenchmarkReport) -> str:
    payload = report.payload()
    duration = payload["results"]["cycleDurationMs"]
    cpu = payload["results"]["batchCpuMs"]
    return "\n".join(
        [
            f"Worker benchmark: {'PASS' if report.passed else 'FAIL'}",
            "Scope: one worker with real media probes; no capacity certification",
            f"Streams: {report.stream_count}",
            f"Measured iterations: {report.iterations}",
            f"Validation minimum attempts/max gap: {report.minimum_validation_attempts}/{report.maximum_validation_gap_cycles} cycles",
            f"Cycle p50/p95/p99: {duration['p50']}/{duration['p95']}/{duration['p99']} ms",
            f"CPU p50/p95/p99: {cpu['p50']}/{cpu['p95']}/{cpu['p99']} ms",
        ]
    )
