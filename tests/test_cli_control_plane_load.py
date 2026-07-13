import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from videosim.cli import main
from videosim.control_plane_load import parse_duration


class ControlPlaneLoadCliTest(unittest.TestCase):
    def test_duration_units_are_bounded(self):
        self.assertEqual(parse_duration("24h"), 86400)
        self.assertEqual(parse_duration("1.5m"), 90)
        with self.assertRaises(ValueError):
            parse_duration("0s")

    @patch("videosim.cli.run_control_plane_load")
    def test_runs_workload_and_writes_json_evidence(self, run):
        report = Mock(passed=True)
        report.to_json.return_value = '{"passed": true}'
        run.return_value = report

        output = io.StringIO()
        with redirect_stdout(output):
            result = main(
                [
                    "control-plane-load",
                    "--database-url",
                    "postgresql://candidate",
                    "--workload",
                    "scale/workloads/f5-1000-candidate.json",
                    "--duration",
                    "24h",
                    "--output",
                    "artifacts/control-plane-load.json",
                    "--json",
                ]
            )

        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue()), {"passed": True})
        run.assert_called_once_with(
            "postgresql://candidate",
            "scale/workloads/f5-1000-candidate.json",
            86400,
            tick_seconds=20,
            output_path="artifacts/control-plane-load.json",
        )


if __name__ == "__main__":
    unittest.main()
