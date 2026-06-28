import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from videosim.cli import main
from videosim.soak import REQUIRED_SOAK_REPORTS, SoakReport, check_reports, human_summary, run_soak

ROOT = Path(__file__).resolve().parents[1]


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

    def test_check_reports_passes_complete_soak_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in REQUIRED_SOAK_REPORTS:
                (Path(tmp) / f"{name}.json").write_text(
                    json.dumps({"passed": True, "crashes": 0, "validations": 2, "memory_growth_mb": 1.0}),
                    encoding="utf-8",
                )

            report = check_reports(tmp, memory_growth_threshold_mb=200)

        self.assertTrue(report.passed)
        self.assertEqual(report.checked_reports, len(REQUIRED_SOAK_REPORTS))

    def test_check_reports_fails_missing_or_bad_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "normal.json").write_text(
                json.dumps({"passed": True, "crashes": 0, "validations": 1, "memory_growth_mb": 250}),
                encoding="utf-8",
            )

            report = check_reports(tmp, memory_growth_threshold_mb=200)

        self.assertFalse(report.passed)
        self.assertTrue(any("missing report" in error for error in report.errors))
        self.assertTrue(any("memory_growth_mb=250" in error for error in report.errors))

    def test_cli_soak_check_returns_success_for_complete_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in REQUIRED_SOAK_REPORTS:
                (Path(tmp) / f"{name}.json").write_text(
                    json.dumps({"passed": True, "crashes": 0, "validations": 1}),
                    encoding="utf-8",
                )

            with redirect_stdout(StringIO()):
                code = main(["soak-check", "--report-dir", tmp])

        self.assertEqual(code, 0)

    def test_m12_soak_script_covers_required_profiles(self):
        script = ROOT / "scripts" / "run-m12-soak.sh"
        result = subprocess.run(["bash", "-n", str(script)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        self.assertEqual(result.returncode, 0, result.stdout)
        text = script.read_text(encoding="utf-8")
        for profile in (
            "profiles/srt-normal.yaml",
            "profiles/srt-audio-only.yaml",
            "profiles/srt-video-only.yaml",
            "profiles/srt-no-captions.yaml",
            "profiles/srt-black-video.yaml",
            "profiles/srt-frozen-video.yaml",
        ):
            self.assertIn(profile, text)
        self.assertIn("NORMAL_DURATION_SECONDS", text)
        self.assertIn("OUTAGE_DURATION_SECONDS", text)
        self.assertIn("VALIDATION_INTERVAL_SECONDS", text)
        self.assertIn("soak-check", text)


if __name__ == "__main__":
    unittest.main()
