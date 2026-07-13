from __future__ import annotations

import hashlib
import json
import math
import platform
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .control_plane import MAX_REPORT_STREAMS
from .distributed_benchmark import percentile
from .fixture_fleet import BEHAVIORS
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
    validation_outcomes_by_protocol: dict[str, dict[str, int]]
    validation_by_fixture_behavior: dict[str, dict]
    require_full_validation_coverage: bool
    max_validation_gap_cycles: int
    max_validation_gap_seconds: float
    validation_attempted_streams: int
    cycles_to_full_validation_coverage: int | None
    time_to_full_validation_coverage_seconds_upper_bound: float | None
    minimum_validation_attempts: int
    maximum_validation_gap_cycles: int | None
    maximum_validation_gap_seconds_upper_bound: float | None
    measured_duration_seconds: float
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
            and (
                self.max_validation_gap_seconds == 0
                or (
                    self.minimum_validation_attempts >= 2
                    and self.maximum_validation_gap_seconds_upper_bound is not None
                    and self.maximum_validation_gap_seconds_upper_bound
                    <= self.max_validation_gap_seconds
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
                "maxValidationGapSeconds": self.max_validation_gap_seconds,
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
                "validationOutcomesByProtocol": {
                    protocol: dict(sorted(outcomes.items()))
                    for protocol, outcomes in sorted(
                        self.validation_outcomes_by_protocol.items()
                    )
                },
                "validationByFixtureBehavior": self.validation_by_fixture_behavior,
                "validationAttemptedStreams": self.validation_attempted_streams,
                "validationCoveragePercent": self.validation_coverage_percent,
                "cyclesToFullValidationCoverage": self.cycles_to_full_validation_coverage,
                "timeToFullValidationCoverageSecondsUpperBound": rounded(
                    self.time_to_full_validation_coverage_seconds_upper_bound
                ),
                "minimumValidationAttempts": self.minimum_validation_attempts,
                "maximumValidationGapCycles": self.maximum_validation_gap_cycles,
                "maximumValidationGapSecondsUpperBound": rounded(
                    self.maximum_validation_gap_seconds_upper_bound
                ),
                "measuredDurationSeconds": rounded(self.measured_duration_seconds),
                "processPeakRssBytes": self.process_peak_rss_bytes,
                "childPeakRssBytes": self.child_peak_rss_bytes,
                "openFileDescriptors": self.open_file_descriptors,
            },
            "limitations": [
                "Measures one worker against the supplied live endpoints.",
                "Wall-time freshness is a conservative cycle-boundary upper bound, not a per-probe timestamp.",
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


def rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


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
    max_validation_gap_seconds: float = 0,
    *,
    monitor: Callable = run_monitor_once,
    resource_snapshot: Callable[[], dict] = worker_resource_snapshot,
    monotonic: Callable[[], float] = time.perf_counter,
) -> WorkerBenchmarkReport:
    if iterations < 1 or warmup_iterations < 0:
        raise ValueError("iterations must be positive and warmup iterations non-negative")
    if max_validation_gap_cycles < 0:
        raise ValueError("max validation gap cycles must be non-negative")
    if not math.isfinite(max_validation_gap_seconds) or max_validation_gap_seconds < 0:
        raise ValueError("max validation gap seconds must be finite and non-negative")
    scenario, scenario_sha256, stream_count = load_scenario(scenario_path)
    state = empty_monitor_state()
    running_stream_ids = {
        stream["id"]
        for stream in scenario["streams"]
        if stream.get("status") == "running"
    }
    protocol_by_stream_id = {
        stream["id"]: (
            str(stream["protocol"])
            if stream.get("protocol") in {"srt", "dash"}
            else "unknown"
        )
        for stream in scenario["streams"]
        if stream.get("status") == "running"
    }
    fixture_behavior_by_stream_id = {
        stream["id"]: (
            stream["fixtureBehavior"]
            if stream.get("fixtureBehavior") in BEHAVIORS
            else "unknown"
        )
        for stream in scenario["streams"]
        if stream.get("status") == "running"
    }
    durations = []
    cpu = []
    stream_counts = []
    check_counts = []
    outcomes: dict[str, int] = {}
    validation_outcomes_by_protocol: dict[str, dict[str, int]] = {}
    validation_outcomes_by_fixture_behavior: dict[str, dict[str, int]] = {}
    process_peak = 0
    child_peak = 0
    open_fds = None
    validation_attempted_stream_ids = set()
    validation_attempt_counts = {stream_id: 0 for stream_id in running_stream_ids}
    last_validation_attempt_cycles = {}
    last_validation_attempt_cycle_starts = {}
    maximum_validation_gap_cycles = None
    maximum_validation_gap_seconds_upper_bound = None
    cycles_to_full_validation_coverage = None
    time_to_full_validation_coverage_seconds_upper_bound = None
    measured_started = None
    measured_finished = None
    for cycle in range(iterations + warmup_iterations):
        before = resource_snapshot()
        started = monotonic()
        if cycle == warmup_iterations:
            measured_started = started
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
        completed = monotonic()
        elapsed_ms = max(0.0, (completed - started) * 1000)
        resources = worker_resource_pressure(before, resource_snapshot())
        if cycle < warmup_iterations:
            continue
        measured_finished = completed
        metrics = state.get("probeMetrics")
        if not isinstance(metrics, dict):
            raise ValueError("worker benchmark monitor returned no probe metrics")
        durations.append(elapsed_ms)
        cpu.append(resources["lastBatchCpuMs"])
        stream_counts.append(int(metrics.get("streamCount", 0)))
        check_counts.append(int(metrics.get("checkCount", 0)))
        attempted_this_cycle = set()
        for item in metrics.get("streams", []):
            if not isinstance(item, dict) or item.get("check") != "validation":
                continue
            stream_id = item.get("streamId")
            if stream_id not in running_stream_ids:
                continue
            outcome = str(item.get("outcome") or "unknown")
            if outcome not in {"success", "issue", "error", "timeout", "skipped"}:
                outcome = "unknown"
            protocol_outcomes = validation_outcomes_by_protocol.setdefault(
                protocol_by_stream_id[stream_id], {}
            )
            protocol_outcomes[outcome] = protocol_outcomes.get(outcome, 0) + 1
            behavior_outcomes = validation_outcomes_by_fixture_behavior.setdefault(
                fixture_behavior_by_stream_id[stream_id], {}
            )
            behavior_outcomes[outcome] = behavior_outcomes.get(outcome, 0) + 1
            if outcome != "skipped":
                attempted_this_cycle.add(stream_id)
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
            previous_start = last_validation_attempt_cycle_starts.get(stream_id)
            gap_seconds_upper_bound = completed - (
                previous_start if previous_start is not None else measured_started
            )
            maximum_validation_gap_seconds_upper_bound = max(
                maximum_validation_gap_seconds_upper_bound or 0,
                gap_seconds_upper_bound,
            )
            last_validation_attempt_cycles[stream_id] = measured_cycle
            last_validation_attempt_cycle_starts[stream_id] = started
            validation_attempt_counts[stream_id] += 1
        validation_attempted_stream_ids.update(attempted_this_cycle)
        if (
            cycles_to_full_validation_coverage is None
            and validation_attempted_stream_ids == running_stream_ids
        ):
            cycles_to_full_validation_coverage = cycle - warmup_iterations + 1
            time_to_full_validation_coverage_seconds_upper_bound = (
                completed - measured_started
            )
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
        last_start = last_validation_attempt_cycle_starts.get(stream_id)
        trailing_seconds_upper_bound = measured_finished - (
            last_start if last_start is not None else measured_started
        )
        maximum_validation_gap_seconds_upper_bound = max(
            maximum_validation_gap_seconds_upper_bound or 0,
            trailing_seconds_upper_bound,
        )
    validation_by_fixture_behavior = {}
    for behavior in sorted(set(fixture_behavior_by_stream_id.values())):
        stream_ids = {
            stream_id
            for stream_id, value in fixture_behavior_by_stream_id.items()
            if value == behavior
        }
        attempted = len(stream_ids & validation_attempted_stream_ids)
        validation_by_fixture_behavior[behavior] = {
            "streamCount": len(stream_ids),
            "validationAttemptedStreams": attempted,
            "validationCoveragePercent": round(attempted * 100 / len(stream_ids), 3),
            "outcomes": dict(
                sorted(validation_outcomes_by_fixture_behavior.get(behavior, {}).items())
            ),
        }
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
        validation_outcomes_by_protocol=validation_outcomes_by_protocol,
        validation_by_fixture_behavior=validation_by_fixture_behavior,
        require_full_validation_coverage=require_full_validation_coverage,
        max_validation_gap_cycles=max_validation_gap_cycles,
        max_validation_gap_seconds=max_validation_gap_seconds,
        validation_attempted_streams=len(validation_attempted_stream_ids),
        cycles_to_full_validation_coverage=cycles_to_full_validation_coverage,
        time_to_full_validation_coverage_seconds_upper_bound=time_to_full_validation_coverage_seconds_upper_bound,
        minimum_validation_attempts=min(validation_attempt_counts.values()),
        maximum_validation_gap_cycles=maximum_validation_gap_cycles,
        maximum_validation_gap_seconds_upper_bound=maximum_validation_gap_seconds_upper_bound,
        measured_duration_seconds=measured_finished - measured_started,
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
            f"Validation wall-time gap upper bound: {rounded(report.maximum_validation_gap_seconds_upper_bound)} seconds",
            f"Validation outcomes by protocol: {json.dumps(report.validation_outcomes_by_protocol, sort_keys=True)}",
            f"Validation by fixture behavior: {json.dumps(report.validation_by_fixture_behavior, sort_keys=True)}",
            f"Cycle p50/p95/p99: {duration['p50']}/{duration['p95']}/{duration['p99']} ms",
            f"CPU p50/p95/p99: {cpu['p50']}/{cpu['p95']}/{cpu['p99']} ms",
        ]
    )
