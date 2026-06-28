import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from videosim.cli import main
from videosim.feed import video_pipeline_args
from videosim.profile import ProfileError, load_profile


ROOT = Path(__file__).resolve().parents[1]


class ProfileTest(unittest.TestCase):
    def test_sample_normal_profile_loads(self):
        config = load_profile(ROOT / "profiles" / "srt-normal.yaml")

        self.assertEqual(config.port, 9000)
        self.assertTrue(config.audio)
        self.assertTrue(config.captions)
        self.assertEqual(config.audio_frequency, 440)

    def test_required_static_profiles_map_to_expected_state(self):
        cases = {
            "srt-normal.yaml": {"video": True, "audio": True, "captions": True},
            "srt-audio-only.yaml": {"video": False, "audio": True, "captions": False},
            "srt-video-only.yaml": {"video": True, "audio": False, "captions": True},
            "srt-no-captions.yaml": {"video": True, "audio": True, "captions": False},
            "srt-black-video.yaml": {"video": True, "audio": True, "captions": True, "pattern": "black"},
            "srt-frozen-video.yaml": {"video": True, "audio": True, "captions": True, "frozen": True},
        }

        for filename, expected in cases.items():
            with self.subTest(profile=filename):
                config = load_profile(ROOT / "profiles" / filename)
                for field, value in expected.items():
                    self.assertEqual(getattr(config, field), value)

    def test_required_static_profiles_build_expected_pipeline_branches(self):
        with patch("videosim.feed.shutil.which", return_value="/usr/bin/gst-launch-1.0"):
            audio_only = video_pipeline_args(load_profile(ROOT / "profiles" / "srt-audio-only.yaml"))
            video_only = video_pipeline_args(load_profile(ROOT / "profiles" / "srt-video-only.yaml"))
            no_captions = video_pipeline_args(load_profile(ROOT / "profiles" / "srt-no-captions.yaml"))
            black_video = video_pipeline_args(load_profile(ROOT / "profiles" / "srt-black-video.yaml"))
            frozen_video = video_pipeline_args(load_profile(ROOT / "profiles" / "srt-frozen-video.yaml"))

        self.assertNotIn("videotestsrc", audio_only)
        self.assertIn("audiotestsrc", audio_only)
        self.assertIn("videotestsrc", video_only)
        self.assertNotIn("audiotestsrc", video_only)
        self.assertNotIn("h264ccinserter", no_captions)
        self.assertIn("pattern=black", black_video)
        self.assertIn("imagefreeze", frozen_video)

    def test_cli_print_command_uses_profile(self):
        stdout = StringIO()
        with redirect_stdout(stdout), patch("videosim.feed.shutil.which", return_value="/usr/bin/gst-launch-1.0"):
            code = main(["start", "--profile", str(ROOT / "profiles" / "srt-normal.yaml"), "--print-command"])

        self.assertEqual(code, 0)
        self.assertIn("audiotestsrc", stdout.getvalue())
        self.assertIn("h264ccinserter", stdout.getvalue())

    def test_invalid_profile_fails_safely(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as profile:
            profile.write("schema_version: 1\nmode: unsupported\n")
            profile.flush()

            with self.assertRaisesRegex(ProfileError, "Unsupported profile mode"):
                load_profile(profile.name)

    def test_missing_required_field_is_clear(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as profile:
            profile.write("schema_version: 1\n")
            profile.flush()

            with self.assertRaisesRegex(ProfileError, "Missing required profile field: mode"):
                load_profile(profile.name)

    def test_schema_version_is_checked(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as profile:
            profile.write("schema_version: 2\nmode: normal\n")
            profile.flush()

            with self.assertRaisesRegex(ProfileError, "Unsupported profile schema_version"):
                load_profile(profile.name)

    def test_cli_reports_profile_errors(self):
        stderr = StringIO()
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as profile:
            profile.write("schema_version: 2\nmode: normal\n")
            profile.flush()

            with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                main(["start", "--profile", profile.name, "--print-command"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("Unsupported profile schema_version", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
