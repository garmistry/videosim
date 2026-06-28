import json
import unittest

from videosim.validator import ValidationReport, human_summary


class ValidatorOutputTest(unittest.TestCase):
    def test_report_json_is_machine_readable(self):
        report = ValidationReport(endpoint="srt://127.0.0.1:9000?mode=caller", reachable=True, passed=True)

        payload = json.loads(report.to_json())

        self.assertTrue(payload["passed"])
        self.assertEqual(payload["endpoint"], report.endpoint)

    def test_human_summary_includes_errors(self):
        report = ValidationReport(endpoint="srt://127.0.0.1:9000?mode=caller", errors=["feed unreachable"])

        summary = human_summary(report)

        self.assertIn("Validation FAIL", summary)
        self.assertIn("feed unreachable", summary)


if __name__ == "__main__":
    unittest.main()
