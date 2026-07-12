import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from videosim.feed import VideoFeedConfig
from videosim.loudness import LoudnessError, measure_loudness, parse_ebur128_summary
from videosim.probe_deadline import use_probe_deadline


class LoudnessTest(unittest.TestCase):
    def test_stream_budget_caps_and_cancels_ffmpeg(self):
        with use_probe_deadline(time.monotonic() + 1), patch(
            "videosim.loudness.subprocess.run",
            side_effect=subprocess.TimeoutExpired("ffmpeg", 1),
        ) as run:
            with self.assertRaisesRegex(TimeoutError, "stream probe budget exhausted"):
                measure_loudness(VideoFeedConfig())

        self.assertLess(run.call_args.kwargs["timeout"], 1)

    def test_parse_ffmpeg_ebur128_summary(self):
        report = parse_ebur128_summary(
            """
[Parsed_ebur128_0 @ 0x1] Summary:

  Integrated loudness:
    I:         -23.2 LUFS
    Threshold: -33.2 LUFS

  True peak:
    Peak:       -1.4 dBFS
""",
            source="sample.ts",
        )

        self.assertEqual(report.integrated_lufs, -23.2)
        self.assertEqual(report.true_peak_dbtp, -1.4)
        self.assertEqual(report.source, "sample.ts")

    def test_parse_rejects_missing_summary_values(self):
        with self.assertRaises(LoudnessError):
            parse_ebur128_summary("Summary:\n  Integrated loudness:\n    I: -23.2 LUFS")

    def test_dash_config_requires_audio_segment_for_measurement(self):
        with tempfile.TemporaryDirectory() as directory:
            config = VideoFeedConfig(protocol="dash", dash_dir=directory)

            with self.assertRaises(LoudnessError):
                from videosim.loudness import _latest_dash_audio_segment

                _latest_dash_audio_segment(config)

            segment = Path(directory, "audio_0_1.ts")
            segment.write_bytes(b"audio")

            from videosim.loudness import _latest_dash_audio_segment

            self.assertEqual(_latest_dash_audio_segment(config), segment)


if __name__ == "__main__":
    unittest.main()
