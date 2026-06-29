import unittest
from unittest.mock import Mock, patch

from videosim.gui import (
    GuiState,
    MODE_CONTROLS,
    PROFILE_OPTIONS,
    diagnostics_text,
    mode_from_controls,
    mode_from_form,
    render_page,
    state_payload,
)


class GuiTest(unittest.TestCase):
    def test_page_has_start_stop_copyable_endpoint_and_logs(self):
        state = GuiState(feed_port=9912)
        state.log("hello")

        page = render_page(state)

        self.assertIn("Start", page)
        self.assertIn("Stop", page)
        self.assertIn("srt://127.0.0.1:9912?mode=caller", page)
        self.assertIn("hello", page)
        self.assertIn("Last error", page)
        self.assertIn("Validate", page)
        self.assertIn("Download diagnostics", page)
        for label, _ in PROFILE_OPTIONS.values():
            self.assertIn(label, page)
        for label in ("Video", "Audio", "Captions", "Black video", "Frozen video", "Apply controls"):
            self.assertIn(label, page)
        self.assertIn('id="app"', page)
        self.assertIn('/static/app.js', page)

    def test_react_state_payload_exposes_gui_state(self):
        state = GuiState(feed_port=9912, mode="video_only")
        state.log("ready")

        payload = state_payload(state)

        self.assertEqual(payload["endpoint"], "srt://127.0.0.1:9912?mode=caller")
        self.assertEqual(payload["mode"], "video_only")
        self.assertEqual(payload["logs"], ["ready"])
        self.assertFalse(payload["controls"]["audio"])

    def test_start_launches_normal_profile_feed(self):
        state = GuiState(feed_port=9912, width=320, height=180, framerate=10)

        with patch("videosim.gui.subprocess.Popen") as popen:
            popen.return_value.stdout = []
            popen.return_value.poll.return_value = None
            state.start()

        cmd = popen.call_args.args[0]
        self.assertIn("start", cmd)
        self.assertIn("--profile", cmd)
        self.assertIn("profiles/srt-normal.yaml", cmd)
        self.assertIn("--port", cmd)
        self.assertIn("9912", cmd)
        self.assertEqual(state.status, "running")

    def test_start_launches_selected_outage_profile(self):
        state = GuiState(feed_port=9912, mode="black_video")

        with patch("videosim.gui.subprocess.Popen") as popen:
            popen.return_value.stdout = []
            popen.return_value.poll.return_value = None
            state.start()

        cmd = popen.call_args.args[0]
        self.assertIn("profiles/srt-black-video.yaml", cmd)

    def test_unsupported_mode_does_not_start_feed(self):
        state = GuiState(mode="unknown")

        with patch("videosim.gui.subprocess.Popen") as popen:
            state.start()

        popen.assert_not_called()
        self.assertIn("Unsupported mode: unknown", state.logs)

    def test_fault_controls_map_to_supported_modes(self):
        for mode, controls in MODE_CONTROLS.items():
            with self.subTest(mode=mode):
                self.assertEqual(mode_from_controls(controls), mode)

    def test_fault_control_form_maps_missing_checkboxes_to_false(self):
        params = {"video": ["on"], "audio": ["on"]}

        self.assertEqual(mode_from_form(params, "normal"), "no_captions")

    def test_contradictory_fault_controls_are_blocked(self):
        controls = {
            "video": True,
            "audio": True,
            "captions": True,
            "black_video": True,
            "frozen_video": True,
        }

        with self.assertRaisesRegex(ValueError, "cannot both be enabled"):
            mode_from_controls(controls)

        with self.assertRaisesRegex(ValueError, "both be disabled"):
            mode_from_form({"controls": ["1"]}, "normal")

    def test_apply_mode_restarts_running_feed(self):
        state = GuiState(feed_port=9912)

        with patch("videosim.gui.os.killpg") as killpg, patch("videosim.gui.subprocess.Popen") as popen:
            first = popen.return_value
            first.stdout = []
            first.pid = 123
            first.poll.return_value = None
            first.wait.return_value = 0
            state.start()

            second = Mock()
            second.stdout = []
            second.pid = 124
            second.poll.return_value = None
            second.wait.return_value = 0
            popen.return_value = second
            state.apply_mode("no_captions")

        self.assertEqual(state.mode, "no_captions")
        self.assertIn("Restarting feed for no_captions mode", state.logs)
        killpg.assert_called()
        cmd = popen.call_args.args[0]
        self.assertIn("profiles/srt-no-captions.yaml", cmd)

    def test_start_failure_leaves_feed_stopped_with_error_log(self):
        state = GuiState()

        with patch("videosim.gui.subprocess.Popen", side_effect=OSError("missing gst")):
            started = state.apply_mode("normal")

        self.assertFalse(started)
        self.assertEqual(state.status, "stopped")
        self.assertEqual(state.last_error, "Failed to start normal feed: missing gst")
        self.assertIn("Failed to start normal feed: missing gst", state.logs)

    def test_intentional_outage_is_visible_without_error(self):
        state = GuiState(mode="audio_only")

        page = render_page(state)

        self.assertIn("Intentional outage: <strong>yes</strong>", page)
        self.assertIn("Last error: <strong>none</strong>", page)

    def test_pipeline_exit_sets_last_error_and_keeps_logs(self):
        state = GuiState()
        process = Mock()
        process.stdout = ["pipeline failed"]
        process.poll.return_value = 2

        state._capture_logs(process)

        self.assertEqual(state.last_error, "Feed process exited with code 2: pipeline failed")
        self.assertIn("pipeline failed", state.logs)

    def test_validation_output_visible_in_gui(self):
        state = GuiState(feed_port=9912, width=320, height=180, framerate=10)
        result = Mock(returncode=0, stdout="Validation PASS\nreachable=True\n")

        with patch("videosim.gui.subprocess.run", return_value=result) as run:
            passed = state.validate()

        self.assertTrue(passed)
        self.assertIn("Validation PASS", render_page(state))
        cmd = run.call_args.args[0]
        self.assertIn("validate", cmd)
        self.assertIn("profiles/srt-normal.yaml", cmd)
        self.assertIn("9912", cmd)

    def test_validation_failure_sets_actionable_error(self):
        state = GuiState()
        result = Mock(returncode=1, stdout="Validation FAIL\nerrors=feed unreachable\n")

        with patch("videosim.gui.subprocess.run", return_value=result):
            passed = state.validate()

        self.assertFalse(passed)
        self.assertEqual(state.last_error, "errors=feed unreachable")
        self.assertIn("Validation failed", state.logs)

    def test_diagnostics_export_contains_state_validation_and_logs(self):
        state = GuiState(feed_port=9912, mode="black_video")
        state.validation_output = "Validation PASS"
        state.log("Started black_video feed")

        diagnostics = diagnostics_text(state)

        self.assertIn("status=stopped", diagnostics)
        self.assertIn("mode=black_video", diagnostics)
        self.assertIn("intentional_outage=yes", diagnostics)
        self.assertIn("Validation PASS", diagnostics)
        self.assertIn("Started black_video feed", diagnostics)


if __name__ == "__main__":
    unittest.main()
