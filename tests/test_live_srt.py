import os
import signal
import subprocess
import sys
import time
import unittest


LIVE = os.environ.get("VIDEOSIM_LIVE_SRT") == "1"


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
        cases = [
            ("srt-normal.yaml", {"video": True, "audio": True, "captions": True}),
            ("srt-audio-only.yaml", {"video": False, "audio": True, "captions": False}),
            ("srt-video-only.yaml", {"video": True, "audio": False, "captions": True}),
            ("srt-no-captions.yaml", {"video": True, "audio": True, "captions": False}),
            ("srt-black-video.yaml", {"video": True, "audio": True, "captions": True, "black": True}),
            ("srt-frozen-video.yaml", {"video": True, "audio": True, "captions": True, "frozen": True}),
        ]
        for offset, (profile, expected) in enumerate(cases):
            with self.subTest(profile=profile):
                self._validate_profile(profile, 9930 + offset, expected)

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

            uri = f"srt://127.0.0.1:{port}?mode=caller"
            self.assertTrue(self._expected_tracks_present(uri, expected))
            if not expected["video"]:
                self.assertFalse(self._track_present(uri, "video"))
            if not expected["audio"]:
                self.assertFalse(self._track_present(uri, "audio"))
            if not expected["captions"]:
                self.assertFalse(self._captions_present(uri))

            if expected.get("black"):
                frames = self._read_rgb_frames(uri, 3, audio=expected["audio"])
                self.assertGreaterEqual(_black_pixel_ratio(frames), 0.95)
            if expected.get("frozen"):
                frames = self._read_rgb_frames(uri, 3, audio=expected["audio"])
                frame_size = 320 * 180 * 3
                self.assertGreaterEqual(_near_identical_ratio(frames[:frame_size], frames[-frame_size:]), 0.95)
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

    def _track_present(self, uri, track):
        parser = "h264parse" if track == "video" else "aacparse"
        return self._receiver_succeeds(
            [
                "gst-launch-1.0",
                "-q",
                "srtsrc",
                f"uri={uri}",
                "!",
                "tsdemux",
                "name=demux",
                "demux.",
                "!",
                "queue",
                "!",
                parser,
                "!",
                "fakesink",
                "sync=false",
                "num-buffers=5",
            ],
            timeout=8,
        )

    def _expected_tracks_present(self, uri, expected):
        args = [
            "gst-launch-1.0",
            "-q",
            "srtsrc",
            f"uri={uri}",
            "!",
            "tsdemux",
            "name=demux",
        ]
        if expected["video"]:
            args.extend(["demux.", "!", "queue", "!", "h264parse", "!"])
            if expected["captions"]:
                args.extend(
                    [
                        "tee",
                        "name=video",
                        "video.",
                        "!",
                        "queue",
                        "!",
                        "fakesink",
                        "sync=false",
                        "num-buffers=5",
                        "video.",
                        "!",
                        "queue",
                        "!",
                        "h264ccextractor",
                        "!",
                        "fakesink",
                        "sync=false",
                        "num-buffers=3",
                    ]
                )
            else:
                args.extend(["fakesink", "sync=false", "num-buffers=5"])
        if expected["audio"]:
            args.extend(
                [
                    "demux.",
                    "!",
                    "queue",
                    "!",
                    "aacparse",
                    "!",
                    "fakesink",
                    "sync=false",
                    "num-buffers=5",
                ]
            )
        return self._receiver_succeeds(args, timeout=15)

    def _captions_present(self, uri):
        return self._receiver_succeeds(
            [
                "gst-launch-1.0",
                "-q",
                "srtsrc",
                f"uri={uri}",
                "!",
                "tsdemux",
                "name=demux",
                "demux.",
                "!",
                "queue",
                "!",
                "h264parse",
                "!",
                "h264ccextractor",
                "!",
                "fakesink",
                "sync=false",
                "num-buffers=3",
            ],
            timeout=8,
        )

    def _read_rgb_frames(self, uri, count, audio=False):
        frame_size = 320 * 180 * 3
        result = subprocess.run(
            [
                "gst-launch-1.0",
                "-q",
                "srtsrc",
                f"uri={uri}",
                "!",
                "tsdemux",
                "name=demux",
                "demux.",
                "!",
                "queue",
                "!",
                "h264parse",
                "!",
                "avdec_h264",
                "!",
                "videoconvert",
                "!",
                "video/x-raw,format=RGB,width=320,height=180",
                "!",
                "identity",
                f"eos-after={count}",
                "!",
                "fdsink",
                "fd=1",
            ]
            + (
                [
                    "demux.",
                    "!",
                    "queue",
                    "!",
                    "aacparse",
                    "!",
                    "fakesink",
                    "sync=false",
                    "num-buffers=5",
                ]
                if audio
                else []
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=12,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        self.assertGreaterEqual(len(result.stdout), frame_size * count)
        return result.stdout[: frame_size * count]

    def _receiver_succeeds(self, args, timeout):
        try:
            result = subprocess.run(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return False
        return result.returncode == 0

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


def _black_pixel_ratio(frames):
    black = sum(1 for byte in frames if byte < 16)
    return black / len(frames)


def _near_identical_ratio(first, second):
    close = sum(1 for a, b in zip(first, second) if abs(a - b) < 8)
    return close / min(len(first), len(second))


if __name__ == "__main__":
    unittest.main()
