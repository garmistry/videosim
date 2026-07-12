import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from videosim.feed import VideoFeedConfig, video_pipeline_args
from videosim.framerate import (
    FrameRateError,
    frame_rate_float,
    frame_rate_fraction,
    frame_rate_keyint,
    measure_frame_rate,
    normalize_frame_rate,
    parse_ffprobe_frame_rate,
)
from videosim.probe_deadline import use_probe_deadline


class FrameRateTest(unittest.TestCase):
    def test_stream_budget_caps_and_cancels_ffprobe(self):
        with use_probe_deadline(time.monotonic() + 1), patch(
            "videosim.framerate.subprocess.run",
            side_effect=subprocess.TimeoutExpired("ffprobe", 1),
        ) as run:
            with self.assertRaisesRegex(TimeoutError, "stream probe budget exhausted"):
                measure_frame_rate(VideoFeedConfig())

        self.assertLess(run.call_args.kwargs["timeout"], 1)

    def test_supported_broadcast_rates_map_to_gstreamer_fractions(self):
        self.assertEqual(frame_rate_fraction("23.97"), "24000/1001")
        self.assertEqual(frame_rate_fraction("59.94"), "60000/1001")
        self.assertEqual(frame_rate_keyint("59.94"), 60)
        self.assertAlmostEqual(frame_rate_float("23.97"), 23.976, places=3)

    def test_legacy_integer_frame_rates_still_work(self):
        self.assertEqual(normalize_frame_rate(10), "10")
        self.assertEqual(frame_rate_fraction(10), "10/1")

    def test_pipeline_uses_fractional_frame_rate_caps(self):
        args = video_pipeline_args(VideoFeedConfig(width=320, height=180, framerate="59.94"))

        self.assertIn("video/x-raw,width=320,height=180,framerate=60000/1001", args)
        self.assertIn("closedcaption/x-cea-608,format=raw,field=0,framerate=60000/1001", args)
        self.assertIn("key-int-max=60", args)

    def test_parse_ffprobe_frame_rate_json(self):
        self.assertAlmostEqual(parse_ffprobe_frame_rate('{"streams":[{"avg_frame_rate":"60000/1001"}]}'), 59.94, places=2)

        with self.assertRaises(FrameRateError):
            parse_ffprobe_frame_rate('{"streams":[{"avg_frame_rate":"0/0"}]}')

    def test_dash_frame_rate_probe_requires_video_segment(self):
        from videosim.framerate import _frame_rate_input

        with tempfile.TemporaryDirectory() as directory:
            config = VideoFeedConfig(protocol="dash", dash_dir=directory)
            with self.assertRaises(FrameRateError):
                _frame_rate_input(config)

            segment = Path(directory, "video_0_1.ts")
            segment.write_bytes(b"video")
            self.assertEqual(_frame_rate_input(config), segment)


if __name__ == "__main__":
    unittest.main()
