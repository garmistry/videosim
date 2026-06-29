import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

from videosim.cli import main
from videosim.feed import VideoFeedConfig, cea608_pairs, run_video_feed, video_pipeline_args


class VideoFeedCliTest(unittest.TestCase):
    def test_endpoint_uses_caller_mode_for_receivers(self):
        config = VideoFeedConfig(port=9910)

        self.assertEqual(config.endpoint, "srt://127.0.0.1:9910?mode=caller")

    def test_pipeline_uses_srt_listener_video_and_audio_test_sources(self):
        config = VideoFeedConfig(port=9910, width=320, height=180, framerate=10)

        with patch("videosim.feed.shutil.which", return_value="/usr/bin/gst-launch-1.0"):
            args = video_pipeline_args(config)

        self.assertIn("videotestsrc", args)
        self.assertIn("audiotestsrc", args)
        self.assertIn("x264enc", args)
        self.assertIn("clockoverlay", args)
        self.assertIn("time-format=%Y-%m-%d %H:%M:%S", args)
        self.assertIn("avenc_aac", args)
        self.assertIn("freq=440", args)
        self.assertIn("cccombiner", args)
        self.assertIn("h264ccinserter", args)
        self.assertIn("fdsrc", args)
        self.assertIn("closedcaption/x-cea-608,format=raw,field=0,framerate=10/1", args)
        self.assertIn("mpegtsmux", args)
        self.assertIn("srtsink", args)
        self.assertIn("video/x-raw,width=320,height=180,framerate=10/1", args)
        self.assertIn("uri=srt://:9910?mode=listener", args)

    def test_pipeline_can_disable_audio(self):
        config = VideoFeedConfig(port=9910, audio=False)

        with patch("videosim.feed.shutil.which", return_value="/usr/bin/gst-launch-1.0"):
            args = video_pipeline_args(config)

        self.assertNotIn("audiotestsrc", args)

    def test_pipeline_can_disable_captions(self):
        config = VideoFeedConfig(port=9910, captions=False)

        with patch("videosim.feed.shutil.which", return_value="/usr/bin/gst-launch-1.0"):
            args = video_pipeline_args(config)

        self.assertNotIn("h264ccinserter", args)
        self.assertNotIn("fdsrc", args)

    def test_cea608_caption_bytes_have_odd_parity(self):
        pairs = list(cea608_pairs("OK"))

        self.assertEqual(len(pairs), 1)
        for byte in pairs[0]:
            self.assertEqual(byte.bit_count() % 2, 1)

    def test_invalid_port_returns_clear_error(self):
        stderr = StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            main(["start", "--port", "70000"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("port must be between 1 and 65535", stderr.getvalue())

    def test_invalid_audio_frequency_returns_clear_error(self):
        stderr = StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            main(["start", "--audio-frequency", "0"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("audio_frequency must be greater than 0", stderr.getvalue())

    def test_missing_gstreamer_returns_clear_error(self):
        stderr = StringIO()
        with redirect_stderr(stderr), patch("videosim.feed.shutil.which", return_value=None), self.assertRaises(
            SystemExit
        ) as raised:
            main(["start", "--print-command"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("Missing gst-launch-1.0", stderr.getvalue())

    def test_print_command_does_not_start_feed(self):
        stdout = StringIO()
        with redirect_stdout(stdout), patch("videosim.feed.shutil.which", return_value="/usr/bin/gst-launch-1.0"):
            code = main(["start", "--port", "9910", "--print-command"])

        self.assertEqual(code, 0)
        self.assertIn("gst-launch-1.0", stdout.getvalue())
        self.assertIn("srt://127.0.0.1:9910?mode=caller", stdout.getvalue())

    def test_keyboard_interrupt_stops_feed_cleanly(self):
        class FakeProcess:
            def __init__(self):
                self.signals = []
                self.waits = 0

            def wait(self, timeout=None):
                self.waits += 1
                if self.waits == 1:
                    raise KeyboardInterrupt
                return 254

            def send_signal(self, signum):
                self.signals.append(signum)

        fake = FakeProcess()
        stdout = StringIO()
        with redirect_stdout(stdout), patch("videosim.feed.shutil.which", return_value="/usr/bin/gst-launch-1.0"), patch(
            "videosim.feed.subprocess.Popen", return_value=fake
        ):
            code = run_video_feed(VideoFeedConfig(port=9910, captions=False))

        self.assertEqual(code, 0)
        self.assertTrue(fake.signals)
        self.assertIn("Starting SRT video feed", stdout.getvalue())
        self.assertIn("Stopping SRT video feed", stdout.getvalue())

    def test_keyboard_interrupt_kills_stuck_feed(self):
        class FakeProcess:
            def __init__(self):
                self.signals = []
                self.terminated = False
                self.killed = False
                self.waits = 0

            def wait(self, timeout=None):
                self.waits += 1
                if self.waits == 1:
                    raise KeyboardInterrupt
                if self.waits < 4:
                    raise TimeoutError
                return 0

            def send_signal(self, signum):
                self.signals.append(signum)

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.killed = True

        fake = FakeProcess()
        with redirect_stdout(StringIO()), patch("videosim.feed.shutil.which", return_value="/usr/bin/gst-launch-1.0"), patch(
            "videosim.feed.subprocess.Popen", return_value=fake
        ), patch("videosim.feed.subprocess.TimeoutExpired", TimeoutError):
            code = run_video_feed(VideoFeedConfig(port=9910, captions=False))

        self.assertEqual(code, 0)
        self.assertTrue(fake.terminated)
        self.assertTrue(fake.killed)


if __name__ == "__main__":
    unittest.main()
