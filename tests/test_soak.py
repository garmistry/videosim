import json
import unittest
from contextlib import redirect_stderr
from io import StringIO
from unittest.mock import Mock, patch

from videosim.cli import main
from videosim.soak import SoakReport, human_summary, run_soak


class SoakTest(unittest.TestCase):
    def test_report_json_and_summary_include_resource_usage(self):
        report = SoakReport(
            profile="profiles/srt-normal.yaml",
            endpoint="srt://127.0.0.1:9000?mode=caller",
            duration_seconds=60,
            validation_interval_seconds=15,
            validations=4,
            memory_growth_mb=1.25,
            passed=True,
        )

        payload = json.loads(report.to_json())
        summary = human_summary(report)

        self.assertTrue(payload["passed"])
        self.assertIn("Soak PASS", summary)
        self.assertIn("memory_growth_mb=1.25", summary)

    def test_run_soak_starts_feed_validates_and_stops(self):
        sender = Mock(pid=123, returncode=None)
        sender.stdout = Mock()
        sender.poll.return_value = None
        validation = Mock(returncode=0, stdout='{"passed": true}')

        with patch("videosim.soak.subprocess.Popen", return_value=sender) as popen, patch(
            "videosim.soak.subprocess.run", return_value=validation
        ) as run, patch("videosim.soak.sample_rss_mb", side_effect=[10.0, 11.0]), patch(
            "videosim.soak.os.killpg"
        ) as killpg, patch(
            "videosim.soak.time.sleep"
        ), patch(
            "videosim.soak.time.monotonic", side_effect=[0, 0, 0, 2]
        ):
            report = run_soak("profiles/srt-normal.yaml", 9910, 320, 180, 10, 1, 1, startup_seconds=0)

        self.assertTrue(report.passed)
        self.assertEqual(report.validations, 1)
        self.assertEqual(report.memory_growth_mb, 1.0)
        self.assertIn("start", popen.call_args.args[0])
        self.assertIn("validate", run.call_args.args[0])
        killpg.assert_called()

    def test_run_soak_reports_sender_crash(self):
        sender = Mock(pid=123, returncode=2)
        sender.stdout.read.return_value = "boom"
        sender.poll.return_value = 2

        with patch("videosim.soak.subprocess.Popen", return_value=sender), patch(
            "videosim.soak.sample_rss_mb", return_value=10.0
        ), patch("videosim.soak.time.sleep"), patch("videosim.soak.time.monotonic", side_effect=[0, 0, 0, 2]):
            report = run_soak("profiles/srt-normal.yaml", 9910, 320, 180, 10, 1, 1, startup_seconds=0)

        self.assertFalse(report.passed)
        self.assertEqual(report.crashes, 1)
        self.assertIn("sender exited early", report.errors[0])

    def test_cli_rejects_invalid_soak_duration(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as raised:
            main(["soak", "--profile", "profiles/srt-normal.yaml", "--duration-seconds", "0"])

        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
