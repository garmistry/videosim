import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from videosim.cli import main
from videosim.worker_benchmark import run_worker_benchmark


class WorkerBenchmarkTest(unittest.TestCase):
    def write_scenario(self, directory: str, *, running: bool = True) -> Path:
        path = Path(directory) / "state.json"
        path.write_text(
            json.dumps(
                {
                    "streams": [
                        {
                            "id": "stream-1",
                            "protocol": "srt",
                            "status": "running" if running else "stopped",
                        },
                        {
                            "id": "stream-2",
                            "protocol": "dash",
                            "status": "running" if running else "stopped",
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_worker_benchmark_reports_real_probe_scope_and_resources(self):
        snapshots = iter(
            (
                {
                    "totalCpuSeconds": 0,
                    "processPeakRssBytes": 10,
                    "childPeakRssBytes": 20,
                    "openFileDescriptors": 3,
                },
                {
                    "totalCpuSeconds": 0.1,
                    "processPeakRssBytes": 11,
                    "childPeakRssBytes": 21,
                    "openFileDescriptors": 4,
                },
                {
                    "totalCpuSeconds": 1,
                    "processPeakRssBytes": 12,
                    "childPeakRssBytes": 22,
                    "openFileDescriptors": 5,
                },
                {
                    "totalCpuSeconds": 1.25,
                    "processPeakRssBytes": 13,
                    "childPeakRssBytes": 23,
                    "openFileDescriptors": 6,
                },
            )
        )

        def monitor(*_args, **_kwargs):
            return {
                "probeMetrics": {
                    "streamCount": 2,
                    "checkCount": 4,
                    "outcomes": {"success": 3, "timeout": 1},
                    "streams": [
                        {
                            "streamId": "stream-1",
                            "check": "validation",
                            "outcome": "timeout",
                        },
                        {
                            "streamId": "stream-2",
                            "check": "validation",
                            "outcome": "success",
                        },
                    ],
                }
            }

        with tempfile.TemporaryDirectory() as directory:
            scenario = self.write_scenario(directory)
            report = run_worker_benchmark(
                scenario,
                iterations=1,
                warmup_iterations=1,
                srt_host="127.0.0.1",
                max_concurrent_checks=2,
                max_concurrent_deep_checks=1,
                stream_budget_seconds=30,
                deep_check_interval_seconds=60,
                batch_budget_seconds=20,
                monitor=monitor,
                resource_snapshot=lambda: next(snapshots),
                monotonic=iter((0, 0.1, 1, 1.2)).__next__,
            )

        payload = report.payload()
        self.assertTrue(report.passed)
        self.assertEqual(payload["scope"], "single_worker_real_media_probes")
        self.assertTrue(payload["mediaProbesExecuted"])
        self.assertFalse(payload["capacityCertified"])
        self.assertEqual(payload["results"]["cycleDurationMs"]["p50"], 200)
        self.assertEqual(payload["results"]["batchCpuMs"]["p50"], 250)
        self.assertEqual(payload["results"]["processPeakRssBytes"], 13)
        self.assertEqual(payload["results"]["openFileDescriptors"], 6)
        self.assertEqual(payload["results"]["validationCoveragePercent"], 100)
        self.assertEqual(
            payload["results"]["validationOutcomesByProtocol"],
            {"dash": {"success": 1}, "srt": {"timeout": 1}},
        )

    def test_full_validation_coverage_gate_tracks_rotation_across_cycles(self):
        cycle = 0

        def monitor(*_args, **_kwargs):
            nonlocal cycle
            cycle += 1
            attempted = {1, 2} if cycle % 2 else {3, 4}
            streams = [
                {
                    "streamId": f"stream-{index}",
                    "check": "validation",
                    "outcome": "success" if index in attempted else "skipped",
                }
                for index in range(1, 5)
            ]
            return {
                "probeMetrics": {
                    "streamCount": 4,
                    "checkCount": 4,
                    "outcomes": {"success": 2, "skipped": 2},
                    "streams": streams,
                }
            }

        snapshot = {
            "totalCpuSeconds": 0,
            "processPeakRssBytes": 10,
            "childPeakRssBytes": 20,
            "openFileDescriptors": 3,
        }
        with tempfile.TemporaryDirectory() as directory:
            scenario = Path(directory) / "state.json"
            scenario.write_text(
                json.dumps(
                    {
                        "streams": [
                            {"id": f"stream-{index}", "status": "running"}
                            for index in range(1, 5)
                        ]
                    }
                ),
                encoding="utf-8",
            )
            report = run_worker_benchmark(
                scenario,
                4,
                0,
                "127.0.0.1",
                2,
                1,
                1,
                0,
                1,
                True,
                2,
                2.2,
                monitor=monitor,
                resource_snapshot=lambda: snapshot,
                monotonic=iter((0, 0.1, 1, 1.1, 2, 2.1, 3, 3.1)).__next__,
            )

        self.assertTrue(report.passed)
        self.assertEqual(report.validation_attempted_streams, 4)
        self.assertEqual(report.cycles_to_full_validation_coverage, 2)
        self.assertEqual(report.minimum_validation_attempts, 2)
        self.assertEqual(report.maximum_validation_gap_cycles, 2)
        self.assertAlmostEqual(report.maximum_validation_gap_seconds_upper_bound, 2.1)
        self.assertEqual(
            report.validation_outcomes_by_protocol,
            {"unknown": {"skipped": 8, "success": 8}},
        )
        self.assertFalse(replace(report, validation_attempted_streams=3).passed)
        self.assertFalse(replace(report, minimum_validation_attempts=1).passed)
        self.assertFalse(replace(report, maximum_validation_gap_cycles=3).passed)
        self.assertFalse(
            replace(report, maximum_validation_gap_seconds_upper_bound=2.3).passed
        )

    def test_validation_gap_gate_counts_trailing_starvation(self):
        cycle = 0

        def monitor(*_args, **_kwargs):
            nonlocal cycle
            cycle += 1
            attempted = cycle <= 2
            return {
                "probeMetrics": {
                    "streamCount": 2,
                    "checkCount": 2,
                    "outcomes": {
                        "success": 2 if attempted else 0,
                        "skipped": 0 if attempted else 2,
                    },
                    "streams": [
                        {
                            "streamId": f"stream-{index}",
                            "check": "validation",
                            "outcome": "success" if attempted else "skipped",
                        }
                        for index in (1, 2)
                    ],
                }
            }

        snapshot = {
            "totalCpuSeconds": 0,
            "processPeakRssBytes": 10,
            "childPeakRssBytes": 20,
            "openFileDescriptors": 3,
        }
        with tempfile.TemporaryDirectory() as directory:
            report = run_worker_benchmark(
                self.write_scenario(directory),
                5,
                0,
                "127.0.0.1",
                2,
                1,
                1,
                0,
                1,
                True,
                3,
                0.6,
                monitor=monitor,
                resource_snapshot=lambda: snapshot,
                monotonic=iter(index / 10 for index in range(10)).__next__,
            )

        self.assertEqual(report.minimum_validation_attempts, 2)
        self.assertEqual(report.maximum_validation_gap_cycles, 4)
        self.assertAlmostEqual(report.maximum_validation_gap_seconds_upper_bound, 0.7)
        self.assertFalse(report.passed)

    def test_worker_benchmark_rejects_scenario_without_running_streams(self):
        with tempfile.TemporaryDirectory() as directory:
            scenario = self.write_scenario(directory, running=False)
            with self.assertRaisesRegex(ValueError, "no running streams"):
                run_worker_benchmark(
                    scenario, 1, 0, "127.0.0.1", 1, 0, 0, 0, 0
                )

    def test_worker_benchmark_cli_prints_json(self):
        with tempfile.TemporaryDirectory() as directory:
            scenario = self.write_scenario(directory)
            with patch(
                "videosim.cli.run_worker_benchmark"
            ) as benchmark, patch("builtins.print") as output:
                benchmark.return_value = Mock(
                    passed=True,
                    to_json=lambda: '{"passed": true}',
                )
                code = main(
                    [
                        "worker-benchmark",
                        "--scenario",
                        str(scenario),
                        "--max-validation-gap-cycles",
                        "2",
                        "--max-validation-gap-seconds",
                        "3.5",
                        "--json",
                    ]
                )

        self.assertEqual(code, 0)
        self.assertEqual(output.call_args.args[0], '{"passed": true}')
        self.assertEqual(benchmark.call_args.args[-2:], (2, 3.5))


if __name__ == "__main__":
    unittest.main()
