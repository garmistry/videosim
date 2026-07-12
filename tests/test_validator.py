import json
import subprocess
import tempfile
import time
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from videosim.feed import VideoFeedConfig
from videosim.probe_deadline import use_probe_deadline
from videosim.validator import ValidationReport, human_summary, validate_config


class ValidatorOutputTest(unittest.TestCase):
    def test_stream_budget_caps_and_cancels_gstreamer_receiver(self):
        from videosim.validator import _receiver_succeeds

        with use_probe_deadline(time.monotonic() + 1), patch(
            "videosim.validator.subprocess.run",
            side_effect=subprocess.TimeoutExpired("gst-launch-1.0", 1),
        ) as run:
            with self.assertRaisesRegex(TimeoutError, "stream probe budget exhausted"):
                _receiver_succeeds(["gst-launch-1.0"], timeout=15)

        self.assertLess(run.call_args.kwargs["timeout"], 1)

    def test_stream_budget_bounds_dash_polling(self):
        from videosim.validator import _wait_for_dash_manifest

        with tempfile.TemporaryDirectory() as directory, use_probe_deadline(
            time.monotonic() + 0.01
        ):
            with self.assertRaisesRegex(TimeoutError, "stream probe budget exhausted"):
                _wait_for_dash_manifest(
                    VideoFeedConfig(protocol="dash", dash_dir=directory)
                )

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

    def test_dash_validation_reads_manifest_and_segments(self):
        with tempfile.TemporaryDirectory() as dash_dir:
            Path(dash_dir, "manifest.mpd").write_text(
                """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011">
  <Period>
    <AdaptationSet contentType="video"><Representation id="video_0"/></AdaptationSet>
    <AdaptationSet contentType="audio"><Representation id="audio_0"/></AdaptationSet>
    <AdaptationSet contentType="text"><Representation id="caption_0"/></AdaptationSet>
  </Period>
</MPD>""",
                encoding="utf-8",
            )
            Path(dash_dir, "video_0_1.ts").write_bytes(b"video")
            Path(dash_dir, "audio_0_1.ts").write_bytes(b"audio")
            Path(dash_dir, "captions.vtt").write_text("WEBVTT\n\n00:00.000 --> 00:01.000\nVIDEOSIM\n")
            config = VideoFeedConfig(protocol="dash", dash_dir=dash_dir, width=1, height=1)

            with patch("videosim.validator._read_dash_rgb_frames", return_value=bytes([255, 255, 255] * 3)):
                report = validate_config(config)

        self.assertTrue(report.passed)
        self.assertTrue(report.reachable)
        self.assertTrue(report.video_present)
        self.assertTrue(report.audio_present)
        self.assertTrue(report.captions_present)

    def test_dash_validation_detects_audio_only_profile(self):
        with tempfile.TemporaryDirectory() as dash_dir:
            Path(dash_dir, "manifest.mpd").write_text(
                """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011">
  <Period><AdaptationSet contentType="audio"><Representation id="audio_0"/></AdaptationSet></Period>
</MPD>""",
                encoding="utf-8",
            )
            Path(dash_dir, "audio_0_1.ts").write_bytes(b"audio")
            config = VideoFeedConfig(protocol="dash", dash_dir=dash_dir, video=False, audio=True, captions=False)

            report = validate_config(config)

        self.assertTrue(report.passed)
        self.assertFalse(report.video_present)
        self.assertTrue(report.audio_present)
        self.assertFalse(report.captions_present)

    def test_passive_srt_validation_reports_tracks_without_expectation_errors(self):
        config = VideoFeedConfig(external_endpoint="srt://camera.local:9999?mode=caller", passive=True)

        with patch("videosim.validator._track_present", side_effect=lambda _endpoint, track: track == "audio"), patch(
            "videosim.validator._captions_present", return_value=False
        ), patch("videosim.validator._expected_tracks_present") as expected:
            report = validate_config(config)

        expected.assert_not_called()
        self.assertTrue(report.passed)
        self.assertTrue(report.reachable)
        self.assertFalse(report.video_present)
        self.assertTrue(report.audio_present)
        self.assertFalse(report.captions_present)

    def test_external_dash_validation_fetches_manifest_and_segments(self):
        manifest = b"""<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011">
  <Period>
    <AdaptationSet contentType="video">
      <SegmentTemplate media="$RepresentationID$_$Number$.ts" startNumber="1"/>
      <Representation id="video_0"/>
    </AdaptationSet>
    <AdaptationSet contentType="audio">
      <SegmentTemplate media="$RepresentationID$_$Number$.ts" startNumber="1"/>
      <Representation id="audio_0"/>
    </AdaptationSet>
    <AdaptationSet contentType="text"><Representation id="caption_0"/></AdaptationSet>
  </Period>
</MPD>"""
        fetched = []

        class Response(BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                self.close()

        def fake_urlopen(url, timeout=8):
            fetched.append(url)
            return Response(manifest if url.endswith("manifest.mpd") else b"segment")

        config = VideoFeedConfig(protocol="dash", external_endpoint="http://example.test/live/manifest.mpd", width=1, height=1)

        with patch("videosim.validator.urlopen", side_effect=fake_urlopen):
            report = validate_config(config)

        self.assertTrue(report.passed)
        self.assertTrue(report.video_present)
        self.assertTrue(report.audio_present)
        self.assertTrue(report.captions_present)
        self.assertIn("http://example.test/live/video_0_1.ts", fetched)
        self.assertIn("http://example.test/live/audio_0_1.ts", fetched)


if __name__ == "__main__":
    unittest.main()
