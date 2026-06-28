import json
import os
import signal
import subprocess
import sys
import time
import unittest
from urllib import parse, request


LIVE = os.environ.get("VIDEOSIM_LIVE_SRT") == "1"

REQUIRED_MODE_CASES = [
    ("normal", "srt-normal.yaml", {"video": True, "audio": True, "captions": True}),
    ("audio_only", "srt-audio-only.yaml", {"video": False, "audio": True, "captions": False}),
    ("video_only", "srt-video-only.yaml", {"video": True, "audio": False, "captions": True}),
    ("no_captions", "srt-no-captions.yaml", {"video": True, "audio": True, "captions": False}),
    ("black_video", "srt-black-video.yaml", {"video": True, "audio": True, "captions": True, "black": True}),
    ("frozen_video", "srt-frozen-video.yaml", {"video": True, "audio": True, "captions": True, "frozen": True}),
]


@unittest.skipUnless(LIVE, "set VIDEOSIM_LIVE_SRT=1 to run live SRT tests")
class LiveSrtTest(unittest.TestCase):
    def test_receiver_detects_audio_video_captions_and_feed_restarts(self):
        port = 9920
        self._run_once(port)
        self._run_once(port)

    def test_no_captions_mode_has_no_extractable_captions(self):
        port = 9921
        self._run_once(port, extra_sender_args=["--no-captions"], expect_captions=False)

    def test_static_outage_profiles_validate(self):
        for offset, (_, profile, expected) in enumerate(REQUIRED_MODE_CASES):
            with self.subTest(profile=profile):
                self._validate_profile(profile, 9930 + offset, expected)

    def test_receiver_compatibility_required_modes(self):
        for offset, (mode, profile, expected) in enumerate(REQUIRED_MODE_CASES):
            port = 9980 + offset
            with self.subTest(mode=mode):
                sender = self._start_sender(
                    [
                        "--profile",
                        f"profiles/{profile}",
                        "--port",
                        str(port),
                        "--width",
                        "320",
                        "--height",
                        "180",
                        "--framerate",
                        "10",
                    ]
                )
                try:
                    time.sleep(4)
                    if sender.poll() is not None:
                        output = sender.stdout.read() if sender.stdout else ""
                        self.fail(f"sender exited early with {sender.returncode}: {output}")
                    endpoint = f"srt://127.0.0.1:{port}?mode=caller"

                    self._assert_ffprobe_streams(endpoint, expected)
                    self._assert_ffplay_consumes(endpoint)
                    self._assert_gst_receiver_consumes(endpoint, expected)
                finally:
                    self._stop_sender(sender)

    def test_validate_stopped_feed_reports_unreachable(self):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "videosim",
                "validate",
                "--profile",
                "profiles/srt-normal.yaml",
                "--port",
                "9949",
                "--width",
                "320",
                "--height",
                "180",
                "--framerate",
                "10",
                "--json",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=20,
        )
        report = json.loads(result.stdout)

        self.assertEqual(result.returncode, 1)
        self.assertFalse(report["reachable"])

    def test_short_soak_harness_validates_normal_feed(self):
        report = self._run_short_soak("profiles/srt-normal.yaml", 9990)

        self.assertTrue(report["passed"])
        self.assertGreaterEqual(report["validations"], 1)
        self.assertEqual(report["crashes"], 0)

    def test_short_soak_harness_validates_outage_feeds(self):
        for offset, (mode, profile, _) in enumerate(REQUIRED_MODE_CASES[1:]):
            with self.subTest(mode=mode):
                report = self._run_short_soak(f"profiles/{profile}", 9991 + offset)

                self.assertTrue(report["passed"])
                self.assertGreaterEqual(report["validations"], 1)
                self.assertEqual(report["crashes"], 0)

    def test_gui_remains_responsive_while_feed_runs(self):
        http_port = 18083
        feed_port = 9997
        gui = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "videosim",
                "gui",
                "--host",
                "127.0.0.1",
                "--http-port",
                str(http_port),
                "--feed-port",
                str(feed_port),
                "--width",
                "320",
                "--height",
                "180",
                "--framerate",
                "10",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            self._wait_for_http(http_port)
            request.urlopen(f"http://127.0.0.1:{http_port}/start", data=b"", timeout=5).read()
            time.sleep(4)
            for _ in range(3):
                page = request.urlopen(f"http://127.0.0.1:{http_port}/", timeout=2).read().decode("utf-8")
                self.assertIn("Status: <strong>running</strong>", page)
                time.sleep(2)
        finally:
            try:
                request.urlopen(f"http://127.0.0.1:{http_port}/stop", data=b"", timeout=5).read()
            except Exception:
                pass
            self._stop_sender(gui)

    def _run_short_soak(self, profile, port):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "videosim",
                "soak",
                "--profile",
                profile,
                "--port",
                str(port),
                "--width",
                "320",
                "--height",
                "180",
                "--framerate",
                "10",
                "--duration-seconds",
                "8",
                "--validation-interval-seconds",
                "3",
                "--startup-seconds",
                "4",
                "--json",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=35,
        )

        self.assertEqual(result.returncode, 0, result.stdout)
        return json.loads(result.stdout)

    def test_gui_starts_normal_feed_that_validates(self):
        http_port = 18080
        feed_port = 9950
        gui = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "videosim",
                "gui",
                "--host",
                "127.0.0.1",
                "--http-port",
                str(http_port),
                "--feed-port",
                str(feed_port),
                "--width",
                "320",
                "--height",
                "180",
                "--framerate",
                "10",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            self._wait_for_http(http_port)
            request.urlopen(f"http://127.0.0.1:{http_port}/start", data=b"", timeout=5).read()
            time.sleep(4)
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "videosim",
                    "validate",
                    "--profile",
                    "profiles/srt-normal.yaml",
                    "--port",
                    str(feed_port),
                    "--width",
                    "320",
                    "--height",
                    "180",
                    "--framerate",
                    "10",
                    "--json",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=30,
            )
            report = json.loads(result.stdout)

            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertTrue(report["passed"])
        finally:
            try:
                request.urlopen(f"http://127.0.0.1:{http_port}/stop", data=b"", timeout=5).read()
            except Exception:
                pass
            self._stop_sender(gui)

    def test_gui_starts_each_outage_mode_and_validation_matches(self):
        http_port = 18081
        feed_port = 9960
        cases = [
            ("normal", "profiles/srt-normal.yaml"),
            ("audio_only", "profiles/srt-audio-only.yaml"),
            ("video_only", "profiles/srt-video-only.yaml"),
            ("no_captions", "profiles/srt-no-captions.yaml"),
            ("black_video", "profiles/srt-black-video.yaml"),
            ("frozen_video", "profiles/srt-frozen-video.yaml"),
        ]
        gui = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "videosim",
                "gui",
                "--host",
                "127.0.0.1",
                "--http-port",
                str(http_port),
                "--feed-port",
                str(feed_port),
                "--width",
                "320",
                "--height",
                "180",
                "--framerate",
                "10",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            self._wait_for_http(http_port)
            for mode, profile in cases:
                with self.subTest(mode=mode):
                    data = parse.urlencode({"mode": mode}).encode("utf-8")
                    request.urlopen(f"http://127.0.0.1:{http_port}/start", data=data, timeout=5).read()
                    time.sleep(4)
                    result = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "videosim",
                            "validate",
                            "--profile",
                            profile,
                            "--port",
                            str(feed_port),
                            "--width",
                            "320",
                            "--height",
                            "180",
                            "--framerate",
                            "10",
                            "--json",
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        timeout=30,
                    )
                    report = json.loads(result.stdout)
                    self.assertEqual(result.returncode, 0, result.stdout)
                    self.assertTrue(report["passed"])
                    request.urlopen(f"http://127.0.0.1:{http_port}/stop", data=b"", timeout=5).read()
                    time.sleep(0.5)
        finally:
            try:
                request.urlopen(f"http://127.0.0.1:{http_port}/stop", data=b"", timeout=5).read()
            except Exception:
                pass
            self._stop_sender(gui)

    def test_gui_runtime_fault_controls_restart_and_validate(self):
        http_port = 18082
        feed_port = 9970
        normal = {"video": "on", "audio": "on", "captions": "on"}
        transitions = [
            ("video off", {"audio": "on"}, "profiles/srt-audio-only.yaml"),
            ("video on", normal, "profiles/srt-normal.yaml"),
            ("audio off", {"video": "on", "captions": "on"}, "profiles/srt-video-only.yaml"),
            ("audio on", normal, "profiles/srt-normal.yaml"),
            ("captions off", {"video": "on", "audio": "on"}, "profiles/srt-no-captions.yaml"),
            ("captions on", normal, "profiles/srt-normal.yaml"),
            ("black on", {"video": "on", "audio": "on", "captions": "on", "black_video": "on"}, "profiles/srt-black-video.yaml"),
            ("black off", normal, "profiles/srt-normal.yaml"),
            ("frozen on", {"video": "on", "audio": "on", "captions": "on", "frozen_video": "on"}, "profiles/srt-frozen-video.yaml"),
            ("frozen off", normal, "profiles/srt-normal.yaml"),
        ]
        gui = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "videosim",
                "gui",
                "--host",
                "127.0.0.1",
                "--http-port",
                str(http_port),
                "--feed-port",
                str(feed_port),
                "--width",
                "320",
                "--height",
                "180",
                "--framerate",
                "10",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            self._wait_for_http(http_port)
            request.urlopen(
                f"http://127.0.0.1:{http_port}/start",
                data=parse.urlencode(normal).encode("utf-8"),
                timeout=5,
            ).read()
            time.sleep(4)
            self._validate_running_gui_profile("profiles/srt-normal.yaml", feed_port)
            for label, controls, profile in transitions:
                with self.subTest(label=label):
                    request.urlopen(
                        f"http://127.0.0.1:{http_port}/start",
                        data=parse.urlencode(controls).encode("utf-8"),
                        timeout=5,
                    ).read()
                    time.sleep(4)
                    self._validate_running_gui_profile(profile, feed_port)
                    page = request.urlopen(f"http://127.0.0.1:{http_port}/", timeout=5).read().decode("utf-8")
                    mode = profile.removeprefix("profiles/srt-").removesuffix(".yaml").replace("-", "_")
                    self.assertIn(f'<option value="{mode}" selected>', page)
        finally:
            try:
                request.urlopen(f"http://127.0.0.1:{http_port}/stop", data=b"", timeout=5).read()
            except Exception:
                pass
            self._stop_sender(gui)

    def _run_once(self, port, extra_sender_args=None, expect_captions=True):
        extra_sender_args = extra_sender_args or []
        sender = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "videosim",
                "start",
                "--port",
                str(port),
                "--width",
                "320",
                "--height",
                "180",
                "--framerate",
                "10",
            ]
            + extra_sender_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            time.sleep(2)
            if sender.poll() is not None:
                output = sender.stdout.read() if sender.stdout else ""
                self.fail(f"sender exited early with {sender.returncode}: {output}")

            receiver_args = [
                "gst-launch-1.0",
                "-q",
                "srtsrc",
                f"uri=srt://127.0.0.1:{port}?mode=caller",
                "!",
                "tsdemux",
                "name=demux",
                "demux.",
                "!",
                "queue",
                "!",
                "h264parse",
                "!",
            ]
            if expect_captions:
                receiver_args.extend(
                    [
                        "tee",
                        "name=video",
                        "video.",
                        "!",
                        "queue",
                        "!",
                        "fakesink",
                        "sync=false",
                        "num-buffers=10",
                        "video.",
                        "!",
                        "queue",
                        "!",
                        "h264ccextractor",
                        "!",
                        "fakesink",
                        "sync=false",
                        "num-buffers=5",
                    ]
                )
            else:
                receiver_args.extend(["fakesink", "sync=false", "num-buffers=10"])
            receiver_args.extend(
                [
                    "demux.",
                    "!",
                    "queue",
                    "!",
                    "aacparse",
                    "!",
                    "fakesink",
                    "sync=false",
                    "num-buffers=10",
                ]
            )
            receiver = subprocess.run(
                receiver_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=15,
            )
            self.assertEqual(receiver.returncode, 0, receiver.stdout)

            if not expect_captions:
                caption_receiver = [
                    "gst-launch-1.0",
                    "-q",
                    "srtsrc",
                    f"uri=srt://127.0.0.1:{port}?mode=caller",
                    "!",
                    "tsdemux",
                    "!",
                    "h264parse",
                    "!",
                    "h264ccextractor",
                    "!",
                    "fakesink",
                    "sync=false",
                    "num-buffers=1",
                ]
                try:
                    caption_result = subprocess.run(
                        caption_receiver,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        timeout=5,
                    )
                except subprocess.TimeoutExpired:
                    caption_result = None
                if caption_result is not None:
                    self.assertNotEqual(caption_result.returncode, 0, caption_result.stdout)
        finally:
            if sender.poll() is None:
                os.killpg(sender.pid, signal.SIGINT)
                try:
                    sender.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(sender.pid, signal.SIGKILL)
                    sender.wait(timeout=5)
                    self.fail("sender did not stop after SIGINT")
            if sender.stdout:
                sender.stdout.close()

    def _validate_profile(self, profile, port, expected):
        sender = self._start_sender(
            [
                "--profile",
                f"profiles/{profile}",
                "--port",
                str(port),
                "--width",
                "320",
                "--height",
                "180",
                "--framerate",
                "10",
            ]
        )
        try:
            time.sleep(4)
            if sender.poll() is not None:
                output = sender.stdout.read() if sender.stdout else ""
                self.fail(f"sender exited early with {sender.returncode}: {output}")

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "videosim",
                    "validate",
                    "--profile",
                    f"profiles/{profile}",
                    "--port",
                    str(port),
                    "--width",
                    "320",
                    "--height",
                    "180",
                    "--framerate",
                    "10",
                    "--json",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=30,
            )
            report = json.loads(result.stdout)

            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertTrue(report["passed"])
            self.assertEqual(report["video_present"], expected["video"])
            self.assertEqual(report["audio_present"], expected["audio"])
            self.assertEqual(report["captions_present"], expected["captions"])
            if expected.get("black"):
                self.assertTrue(report["black_video"])
            if expected.get("frozen"):
                self.assertTrue(report["frozen_video"])
        finally:
            self._stop_sender(sender)

    def _start_sender(self, args):
        return subprocess.Popen(
            [sys.executable, "-m", "videosim", "start"] + args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )

    def _validate_running_gui_profile(self, profile, port):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "videosim",
                "validate",
                "--profile",
                profile,
                "--port",
                str(port),
                "--width",
                "320",
                "--height",
                "180",
                "--framerate",
                "10",
                "--json",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=45,
        )
        report = json.loads(result.stdout)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(report["passed"])

    def _assert_ffprobe_streams(self, endpoint, expected):
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "json",
                endpoint,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        stream_types = {stream["codec_type"] for stream in json.loads(result.stdout)["streams"]}
        self.assertEqual("video" in stream_types, expected["video"])
        self.assertEqual("audio" in stream_types, expected["audio"])

    def _assert_ffplay_consumes(self, endpoint):
        env = os.environ.copy()
        env.update({"SDL_AUDIODRIVER": "dummy", "SDL_VIDEODRIVER": "dummy"})
        player = subprocess.Popen(
            ["ffplay", "-hide_banner", "-loglevel", "error", "-autoexit", "-t", "3", endpoint],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        time.sleep(5)
        if player.poll() not in (None, 0):
            output = player.stdout.read() if player.stdout else ""
            self.fail(f"ffplay exited early with {player.returncode}: {output}")
        if player.poll() is None:
            player.terminate()
            try:
                player.wait(timeout=5)
            except subprocess.TimeoutExpired:
                player.kill()
                player.wait(timeout=5)
        if player.stdout:
            player.stdout.close()

    def _assert_gst_receiver_consumes(self, endpoint, expected):
        args = ["gst-launch-1.0", "-q", "srtsrc", f"uri={endpoint}", "!", "tsdemux", "name=demux"]
        if expected["video"]:
            args.extend(["demux.", "!", "queue", "!", "h264parse", "!", "fakesink", "sync=false", "num-buffers=5"])
        if expected["audio"]:
            args.extend(["demux.", "!", "queue", "!", "aacparse", "!", "fakesink", "sync=false", "num-buffers=5"])
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout)

    def _wait_for_http(self, port):
        deadline = time.time() + 10
        last_error = None
        while time.time() < deadline:
            try:
                request.urlopen(f"http://127.0.0.1:{port}/", timeout=1).read()
                return
            except Exception as exc:
                last_error = exc
                time.sleep(0.2)
        self.fail(f"GUI did not start: {last_error}")

    def _stop_sender(self, sender):
        if sender.poll() is None:
            os.killpg(sender.pid, signal.SIGINT)
            try:
                sender.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(sender.pid, signal.SIGKILL)
                sender.wait(timeout=5)
                self.fail("sender did not stop after SIGINT")
        if sender.stdout:
            sender.stdout.close()

if __name__ == "__main__":
    unittest.main()
