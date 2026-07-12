import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

from videosim.cli import main
from videosim.feed import SRT_CALLER_LIMIT, VideoFeedConfig, cea608_pairs, run_video_feed, video_pipeline_args


class VideoFeedCliTest(unittest.TestCase):
    def test_monitor_history_prune_runs_bounded_batches_once(self):
        class Store:
            def __init__(self):
                self.batches = [2, 0]
                self.calls = []
                self.closed = False

            def prune_expired_alarm_events(self, *, batch_size):
                self.calls.append(batch_size)
                return self.batches.pop(0)

            def close(self):
                self.closed = True

        store = Store()
        output = StringIO()
        with patch("videosim.cli.configured_database_url", return_value="postgresql://db/videosim"), patch(
            "videosim.cli.PostgresControlPlaneStore", return_value=store
        ), redirect_stdout(output):
            code = main(
                [
                    "monitor-history-prune",
                    "--once",
                    "--batch-size",
                    "2",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(store.calls, [2, 2])
        self.assertTrue(store.closed)
        self.assertIn("Pruned 2 expired monitor event rows", output.getvalue())

    def test_endpoint_uses_caller_mode_for_receivers(self):
        config = VideoFeedConfig(port=9910)

        self.assertEqual(config.endpoint, "srt://127.0.0.1:9910?mode=caller")

    def test_endpoint_can_use_external_override(self):
        config = VideoFeedConfig(protocol="dash", external_endpoint="https://example.test/live/manifest.mpd")

        self.assertEqual(config.endpoint, "https://example.test/live/manifest.mpd")

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
        self.assertIn("wait-for-connection=false", args)
        self.assertFalse(any("maxconn=" in arg for arg in args))
        self.assertLess(args.index("video/x-raw,framerate=10/1"), args.index("cccombiner"))

    def test_pipeline_accepts_fractional_broadcast_frame_rate(self):
        config = VideoFeedConfig(port=9910, width=320, height=180, framerate="59.94")

        with patch("videosim.feed.shutil.which", return_value="/usr/bin/gst-launch-1.0"):
            args = video_pipeline_args(config)

        self.assertIn("video/x-raw,width=320,height=180,framerate=60000/1001", args)
        self.assertIn("key-int-max=60", args)

    def test_frozen_pipeline_uses_static_live_source(self):
        for protocol in ("srt", "dash"):
            with self.subTest(protocol=protocol), patch("videosim.feed.shutil.which", return_value="/usr/bin/gst-launch-1.0"):
                args = video_pipeline_args(VideoFeedConfig(protocol=protocol, port=9910, frozen=True))

            self.assertIn("videotestsrc", args)
            self.assertNotIn("imagefreeze", args)
            self.assertNotIn("num-buffers=1", args)
            self.assertNotIn("clockoverlay", args)
            self.assertIn("pass=quant", args)
            self.assertIn("quantizer=0", args)

    def test_dash_endpoint_uses_manifest_file_by_default(self):
        config = VideoFeedConfig(protocol="dash", dash_dir="/tmp/videosim-test-dash")

        self.assertTrue(config.endpoint.endswith("/videosim-test-dash/manifest.mpd"))
        self.assertTrue(config.endpoint.startswith("file://"))

    def test_dash_endpoint_uses_base_url_when_provided(self):
        config = VideoFeedConfig(protocol="dash", dash_base_url="http://127.0.0.1:8080/dash")

        self.assertEqual(config.endpoint, "http://127.0.0.1:8080/dash/manifest.mpd")

    def test_pipeline_can_generate_dash_audio_video_and_caption_segments(self):
        config = VideoFeedConfig(protocol="dash", port=9910, width=320, height=180, framerate=10, dash_dir="/tmp/dash")

        with patch("videosim.feed.shutil.which", return_value="/usr/bin/gst-launch-1.0"):
            args = video_pipeline_args(config)

        self.assertIn("dashsink", args)
        self.assertIn("dynamic=true", args)
        self.assertIn("mpd-root-path=/tmp/dash", args)
        self.assertIn("mpd-filename=manifest.mpd", args)
        self.assertIn("dash.video_0", args)
        self.assertIn("dash.audio_0", args)
        self.assertNotIn("h264ccinserter", args)
        self.assertNotIn("srtsink", args)

    def test_dash_pipeline_supports_audio_only_fault(self):
        config = VideoFeedConfig(protocol="dash", audio=True, video=False, captions=False)

        with patch("videosim.feed.shutil.which", return_value="/usr/bin/gst-launch-1.0"):
            args = video_pipeline_args(config)

        self.assertIn("dash.audio_0", args)
        self.assertNotIn("dash.video_0", args)

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

    def test_worker_command_runs_control_plane_worker(self):
        with patch("videosim.cli.run_worker", return_value=0) as worker:
            code = main(
                [
                    "worker",
                    "--control-plane-url",
                    "http://master:8080",
                    "--worker-id",
                    "worker-a",
                    "--poll-interval-seconds",
                    "1",
                    "--repeat-interval-seconds",
                    "2",
                    "--history-limit",
                    "3",
                    "--srt-host",
                    "app",
                    "--heartbeat-interval-seconds",
                    "10",
                    "--once",
                ]
            )

        self.assertEqual(code, 0)
        worker.assert_called_once_with(
            "http://master:8080",
            "worker-a",
            1.0,
            2.0,
            3,
            "app",
            True,
            10.0,
            None,
            5,
            0.25,
        )

    def test_verbose_feed_logs_pipeline_command_and_pid(self):
        class FakeProcess:
            pid = 1234
            stdin = None

            def wait(self, timeout=None):
                return 0

        stdout = StringIO()
        with redirect_stdout(stdout), patch.dict("videosim.feed.os.environ", {"VIDEOSIM_VERBOSE": "1"}), patch(
            "videosim.feed.shutil.which", return_value="/usr/bin/gst-launch-1.0"
        ), patch("videosim.feed.subprocess.Popen", return_value=FakeProcess()):
            code = run_video_feed(VideoFeedConfig(port=9910, captions=False))

        self.assertEqual(code, 0)
        self.assertIn("Feed config:", stdout.getvalue())
        self.assertIn(f"srt_caller_limit={SRT_CALLER_LIMIT}", stdout.getvalue())
        self.assertIn("GStreamer command:", stdout.getvalue())
        self.assertIn("SRT feed subprocess pid=1234", stdout.getvalue())

    def test_dash_feed_prepares_output_directory_before_launch(self):
        class FakeProcess:
            pid = 1234
            stdin = None

            def wait(self, timeout=None):
                return 0

        with tempfile.TemporaryDirectory() as dash_dir:
            stale = f"{dash_dir}/video_0_1.ts"
            with open(stale, "wb") as handle:
                handle.write(b"old")
            stdout = StringIO()
            with redirect_stdout(stdout), patch("videosim.feed.shutil.which", return_value="/usr/bin/gst-launch-1.0"), patch(
                "videosim.feed.subprocess.Popen", return_value=FakeProcess()
            ):
                code = run_video_feed(VideoFeedConfig(protocol="dash", dash_dir=dash_dir, captions=False))

        self.assertEqual(code, 0)
        self.assertIn("Starting DASH video feed", stdout.getvalue())

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

    def test_keyboard_interrupt_ignores_broken_stdin_close(self):
        class BrokenStdin:
            def close(self):
                raise BrokenPipeError("caption pipe already closed")

        class FakeProcess:
            stdin = BrokenStdin()

            def __init__(self):
                self.waits = 0

            def wait(self, timeout=None):
                self.waits += 1
                if self.waits == 1:
                    raise KeyboardInterrupt
                return 0

            def send_signal(self, signum):
                pass

        with redirect_stdout(StringIO()), patch("videosim.feed.shutil.which", return_value="/usr/bin/gst-launch-1.0"), patch(
            "videosim.feed.subprocess.Popen", return_value=FakeProcess()
        ):
            code = run_video_feed(VideoFeedConfig(port=9910, captions=False))

        self.assertEqual(code, 0)

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
