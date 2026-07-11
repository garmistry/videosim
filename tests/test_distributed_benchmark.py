import json
import unittest
from unittest.mock import patch

from videosim.cli import main
from videosim.distributed_benchmark import (
    BenchmarkInvariantError,
    assert_assignment_invariants,
    percentile,
    run_control_plane_benchmark,
)


class DistributedBenchmarkTest(unittest.TestCase):
    def test_benchmark_proves_assignment_and_report_invariants_without_media(self):
        report = run_control_plane_benchmark(
            stream_count=12,
            worker_count=3,
            iterations=2,
            warmup_iterations=1,
            seed=17,
        )
        payload = report.payload()

        self.assertTrue(report.passed)
        self.assertEqual(payload["scope"], "in_process_control_plane_only")
        self.assertFalse(payload["mediaProbesExecuted"])
        self.assertFalse(payload["capacityCertified"])
        self.assertEqual(payload["results"]["assignmentsPerWorker"], {"worker-1": 4, "worker-2": 4, "worker-3": 4})
        self.assertEqual(payload["results"]["reportsAccepted"], 6)
        self.assertEqual(len(report.cycle_durations_ms), 2)

    def test_assignment_invariant_rejects_duplicate_owner(self):
        assignments = {
            "worker-1": {
                "assignmentGeneration": 1,
                "controlPlaneInstanceId": "master",
                "assignmentToken": "one",
                "streams": [{"id": "stream-1"}],
            },
            "worker-2": {
                "assignmentGeneration": 1,
                "controlPlaneInstanceId": "master",
                "assignmentToken": "two",
                "streams": [{"id": "stream-1"}],
            },
        }

        with self.assertRaises(BenchmarkInvariantError):
            assert_assignment_invariants(assignments, {"stream-1"})

    def test_percentile_interpolates_sorted_values(self):
        self.assertEqual(percentile([1.0, 2.0, 3.0], 0.5), 2.0)
        self.assertEqual(percentile([1.0], 0.99), 1.0)

    def test_benchmark_cli_prints_json_report(self):
        with patch("builtins.print") as output:
            code = main(
                [
                    "control-plane-benchmark",
                    "--streams",
                    "8",
                    "--workers",
                    "2",
                    "--iterations",
                    "1",
                    "--warmup-iterations",
                    "0",
                    "--seed",
                    "3",
                    "--json",
                ]
            )

        self.assertEqual(code, 0)
        payload = json.loads(output.call_args.args[0])
        self.assertTrue(payload["passed"])
        self.assertEqual(payload["workload"]["streams"], 8)
        self.assertFalse(payload["capacityCertified"])

    def test_benchmark_cli_rejects_non_positive_workload(self):
        with patch("sys.stderr"), self.assertRaises(SystemExit) as raised:
            main(["control-plane-benchmark", "--streams", "0"])

        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
