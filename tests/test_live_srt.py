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


if __name__ == "__main__":
    unittest.main()
