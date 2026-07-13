import json
import tempfile
import unittest
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
                        {"id": "stream-1", "status": "running" if running else "stopped"},
                        {"id": "stream-2", "status": "running" if running else "stopped"},
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
                    "outcomes": {"success": 4},
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
                    ["worker-benchmark", "--scenario", str(scenario), "--json"]
                )

        self.assertEqual(code, 0)
        self.assertEqual(output.call_args.args[0], '{"passed": true}')


if __name__ == "__main__":
    unittest.main()
