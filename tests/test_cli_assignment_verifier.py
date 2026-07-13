import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from videosim.cli import main


class AssignmentVerifierCliTest(unittest.TestCase):
    @patch("videosim.cli.run_assignment_verification")
    def test_writes_machine_readable_assignment_evidence(self, run):
        report = Mock(passed=True)
        report.to_json.return_value = '{"passed": true}'
        run.return_value = report

        output = io.StringIO()
        with redirect_stdout(output):
            result = main(
                [
                    "verify-assignments",
                    "--database-url",
                    "postgresql://candidate",
                    "--workload",
                    "scale/workloads/f5-1000-candidate.json",
                    "--output",
                    "artifacts/assignments.json",
                    "--json",
                ]
            )

        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue()), {"passed": True})
        run.assert_called_once_with(
            "postgresql://candidate",
            "scale/workloads/f5-1000-candidate.json",
            baseline_path="",
            output_path="artifacts/assignments.json",
        )


if __name__ == "__main__":
    unittest.main()
