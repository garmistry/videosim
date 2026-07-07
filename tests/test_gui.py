import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from videosim.gui import (
    GuiState,
    MODE_CONTROLS,
    PROFILE_OPTIONS,
    PROTOCOL_OPTIONS,
    diagnostics_text,
    mode_from_controls,
    mode_from_form,
    profile_for,
    preview_image,
    render_page,
    rgb_frame_to_bmp,
    state_payload,
)


class GuiTest(unittest.TestCase):
    def create_feed(self, state: GuiState, name: str = "Primary feed"):
        return state.create_stream(
            name=name,
            protocol=state.protocol,
            mode=state.mode if state.mode in PROFILE_OPTIONS else "normal",
            feed_port=state.feed_port,
            width=state.width,
            height=state.height,
            framerate=state.framerate,
        )

    def test_default_gui_state_has_no_feeds(self):
        state = GuiState(feed_port=9912)

        payload = state_payload(state)

        self.assertEqual(payload["streams"], [])
        self.assertEqual(payload["selectedStreamId"], "")
        self.assertEqual(payload["status"], "stopped")
        self.assertEqual(payload["endpoint"], "")
        self.assertFalse(payload["previewAvailable"])
        self.assertEqual(payload["feedListUrl"], "/")

    def test_page_has_start_stop_copyable_endpoint_and_logs(self):
        state = GuiState(feed_port=9912)
        self.create_feed(state)
        state.log("hello")

        page = render_page(state)

        self.assertIn("Start", page)
        self.assertIn("Stop", page)
        self.assertIn("srt://127.0.0.1:9912?mode=caller", page)
        self.assertIn("Protocol", page)
        for label in PROTOCOL_OPTIONS.values():
            self.assertIn(label, page)
        self.assertIn("hello", page)
        self.assertIn("Last error", page)
        self.assertIn("Validate", page)
        self.assertIn("Download diagnostics", page)
        self.assertNotIn('action="/streams/create"', page)
        for label, _ in PROFILE_OPTIONS.values():
            self.assertIn(label, page)
        for label in ("Video", "Audio", "Captions", "Black video", "Frozen video", "Apply controls"):
            self.assertIn(label, page)
        self.assertIn('id="app"', page)
        self.assertIn('/static/app.js', page)

    def test_page_has_create_feed_workflow_when_no_feed_selected(self):
        state = GuiState(feed_port=9912)

        page = render_page(state)

        self.assertIn('action="/streams/create"', page)
        self.assertIn("Create stream", page)
        self.assertIn("Open an existing feed or create a new one.", page)
        self.assertNotIn("srt://127.0.0.1:9912?mode=caller", page)

    def test_react_state_payload_exposes_gui_state(self):
        state = GuiState(feed_port=9912, mode="video_only")
        self.create_feed(state)
        state.log("ready")

        payload = state_payload(state)

        self.assertEqual(payload["endpoint"], "srt://127.0.0.1:9912?mode=caller")
        self.assertEqual(payload["protocol"], "srt")
        self.assertIn({"value": "dash", "label": "DASH"}, payload["protocols"])
        self.assertEqual(payload["mode"], "video_only")
        self.assertEqual(payload["logs"], ["ready"])
        self.assertFalse(payload["controls"]["audio"])
        self.assertEqual(payload["previewUrl"], "/preview.jpg")
        self.assertFalse(payload["previewAvailable"])
        self.assertIn("metrics", payload)
        self.assertIn("bitrateBps", payload["streams"][0]["metrics"])
        self.assertEqual(payload["streams"][0]["framerate"], "59.94")
        self.assertEqual(payload["framerate"], "59.94")
        self.assertIn({"value": "59.94", "label": "59.94 fps"}, payload["framerates"])
        self.assertEqual(payload["streams"][0]["previewUrl"], "/feeds/stream-1/preview.jpg")
        self.assertFalse(payload["streams"][0]["previewAvailable"])

    def test_gui_payload_and_fallback_render_monitor_alarms(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor.json"
            path.write_text(
                json.dumps(
                    {
                        "updatedAt": "2026-07-07T00:00:00Z",
                        "alarms": [
                            {
                                "id": "stream-1:essence_video_present",
                                "streamId": "stream-1",
                                "streamName": "Primary feed",
                                "monitorName": "Video present",
                                "severity": "critical",
                                "status": "active",
                                "active": True,
                                "message": "Expected video is absent",
                            }
                        ],
                        "events": [{"id": "1", "streamId": "stream-1", "type": "alarm_raised"}],
                        "monitors": [],
                    }
                ),
                encoding="utf-8",
            )
            state = GuiState(feed_port=9912, monitor_state_path=str(path))
            self.create_feed(state)

            payload = state_payload(state)
            page = render_page(state)

        self.assertEqual(payload["monitor"]["alarms"][0]["monitorName"], "Video present")
        self.assertIn("Monitor alarms (1 active)", page)
        self.assertIn("Expected video is absent", page)

    def test_stream_metrics_payload_updates_for_each_running_feed(self):
        state = GuiState(feed_port=9912, width=320, height=180, framerate=10)
        self.create_feed(state)
        state.create_stream(name="Audio", protocol="srt", mode="audio_only")
        first = Mock(stdout=[], pid=123)
        first.poll.return_value = None
        second = Mock(stdout=[], pid=124)
        second.poll.return_value = None

        with patch("videosim.gui.subprocess.Popen", side_effect=[first, second]), patch(
            "videosim.gui.time.monotonic", side_effect=[100.0, 100.0]
        ):
            state.start("stream-1")
            state.start("stream-2")

        with patch("videosim.gui.time.monotonic", return_value=110.0):
            payload = state_payload(state)

        first_metrics = payload["streams"][0]["metrics"]
        second_metrics = payload["streams"][1]["metrics"]
        self.assertEqual(first_metrics["uptimeSeconds"], 10.0)
        self.assertEqual(second_metrics["uptimeSeconds"], 10.0)
        self.assertGreater(first_metrics["bitrateBps"], second_metrics["bitrateBps"])
        self.assertEqual(first_metrics["outboundBytes"], int(first_metrics["bitrateBps"] * 10 / 8))
        self.assertEqual(second_metrics["videoFrames"], 0)
        self.assertTrue(payload["streams"][0]["previewAvailable"])
        self.assertFalse(payload["streams"][1]["previewAvailable"])

    def test_frontend_renders_feed_table_modal_and_full_preview(self):
        source = Path("frontend/src/main.jsx").read_text()

        self.assertIn('fetch("/state.json"', source)
        self.assertIn("setInterval(refreshState, 1000)", source)
        self.assertIn("FeedTable", source)
        self.assertIn("CreateFeedDialog", source)
        self.assertIn("FullPreviewDialog", source)
        self.assertIn("preview-thumb", source)
        self.assertIn('name="framerate"', source)
        self.assertIn("state.framerates", source)

    def test_frontend_renders_stream_detail_traffic_graphs(self):
        source = Path("frontend/src/main.jsx").read_text()
        style = Path("frontend/src/style.css").read_text()

        self.assertIn("const METRICS_WINDOW_MS = 5 * 60 * 1000", source)
        self.assertIn("setMetricHistory", source)
        self.assertIn("metricSamples={metricHistory[selectedStream.id] || []}", source)
        self.assertIn("Traffic - last 5 min", source)
        self.assertIn("MetricChart", source)
        self.assertIn("MonitorPanel", source)
        self.assertIn("Event audit", source)
        self.assertIn('valueKey="bitrateBps"', source)
        self.assertIn('valueKey="outboundBytes"', source)
        self.assertIn("-5 min", source)
        self.assertIn(".metric-chart", style)
        self.assertIn(".chart-line", style)

    def test_frontend_uses_design_system_tokens(self):
        source = Path("frontend/src/main.jsx").read_text()
        style = Path("frontend/src/style.css").read_text()

        self.assertIn('document.documentElement.dataset.theme', source)
        self.assertIn("StatusBadge", source)
        self.assertIn("EndpointField", source)
        self.assertIn('--font-sans: "IBM Plex Sans"', style)
        self.assertIn("--font-mono: \"IBM Plex Mono\"", style)
        self.assertIn("--accent: oklch(0.76 0.145 70)", style)
        self.assertIn("--status-fault", style)
        self.assertIn("--surface-inset", style)
        self.assertNotIn("#b81d24", style)

    def test_dash_state_payload_exposes_http_manifest_endpoint(self):
        state = GuiState(protocol="dash", http_port=18100)
        self.create_feed(state)

        payload = state_payload(state)

        self.assertEqual(payload["endpoint"], "http://127.0.0.1:18100/dash/stream-1/manifest.mpd")
        self.assertEqual(payload["protocol"], "dash")

    def test_streams_can_be_created_listed_and_selected(self):
        state = GuiState(feed_port=9912)
        self.create_feed(state)
        created = state.create_stream(name="Dash B", protocol="dash", mode="black_video")

        payload = state_payload(state)

        self.assertEqual(state.selected_stream_id, created.id)
        self.assertEqual(len(payload["streams"]), 2)
        self.assertEqual(payload["streams"][1]["name"], "Dash B")
        self.assertEqual(payload["streams"][1]["protocol"], "dash")
        self.assertEqual(payload["streams"][1]["url"], "/feeds/stream-2")
        self.assertEqual(payload["endpoint"], f"http://127.0.0.1:8080/dash/{created.id}/manifest.mpd")

        state.select_stream("stream-1")

        self.assertEqual(state.endpoint, "srt://127.0.0.1:9912?mode=caller")

    def test_render_page_links_to_feed_detail(self):
        state = GuiState(feed_port=9912)
        self.create_feed(state)
        state.clear_selection()

        page = render_page(state)

        self.assertIn('href="/feeds/stream-1"', page)
        self.assertIn('src="/feeds/stream-1/preview.jpg"', page)
        self.assertIn("Active feeds", page)
        self.assertIn('action="/streams/create"', page)
        self.assertNotIn("Selected name", page)

    def test_stream_update_changes_selected_stream_configuration(self):
        state = GuiState(feed_port=9912)
        self.create_feed(state)

        updated = state.update_stream("stream-1", name="Updated", protocol="dash", mode="video_only", framerate="59.94")

        self.assertTrue(updated)
        self.assertEqual(state.active_stream.name, "Updated")
        self.assertEqual(state.protocol, "dash")
        self.assertEqual(state.mode, "video_only")
        self.assertEqual(state.framerate, "59.94")

    def test_create_stream_supports_selected_frame_rate(self):
        state = GuiState(feed_port=9912)
        created = state.create_stream(name="Rate", framerate="23.97")

        self.assertEqual(created.framerate, "23.97")
        self.assertEqual(state_payload(state)["streams"][0]["framerateLabel"], "23.97 fps")

        with patch("videosim.gui.subprocess.Popen") as popen:
            popen.return_value.stdout = []
            popen.return_value.pid = 1234
            popen.return_value.poll.return_value = None
            state.start(created.id)

        cmd = popen.call_args.args[0]
        self.assertIn("--framerate", cmd)
        self.assertIn("23.97", cmd)

    def test_stream_delete_stops_and_removes_record(self):
        state = GuiState(feed_port=9912)
        self.create_feed(state)
        state.create_stream(name="Delete me", protocol="srt", mode="normal")

        deleted = state.delete_stream("stream-2")

        self.assertTrue(deleted)
        self.assertNotIn("stream-2", state.streams)
        self.assertEqual(state.selected_stream_id, "stream-1")

    def test_multiple_streams_start_independent_processes(self):
        state = GuiState(feed_port=9912, width=320, height=180, framerate=10)
        self.create_feed(state)
        state.create_stream(name="Dash", protocol="dash", mode="normal")

        first = Mock()
        first.stdout = []
        first.pid = 123
        first.poll.return_value = None
        second = Mock()
        second.stdout = []
        second.pid = 124
        second.poll.return_value = None

        with patch("videosim.gui.subprocess.Popen", side_effect=[first, second]) as popen:
            state.start("stream-1")
            state.start("stream-2")

        self.assertEqual(state.streams["stream-1"].process, first)
        self.assertEqual(state.streams["stream-2"].process, second)
        self.assertEqual(popen.call_count, 2)
        self.assertIn("9912", popen.call_args_list[0].args[0])
        self.assertIn("--dash-dir", popen.call_args_list[1].args[0])

    def test_react_state_marks_running_video_feed_preview_available(self):
        state = GuiState(feed_port=9912)
        self.create_feed(state)
        state.process = Mock()
        state.process.poll.return_value = None

        payload = state_payload(state)

        self.assertTrue(payload["previewAvailable"])

    def test_react_state_blocks_preview_for_audio_only_feed(self):
        state = GuiState(feed_port=9912, mode="audio_only")
        self.create_feed(state)
        state.process = Mock()
        state.process.poll.return_value = None

        payload = state_payload(state)

        self.assertFalse(payload["previewAvailable"])

    def test_preview_image_grabs_jpeg_frame_from_running_srt_feed(self):
        state = GuiState(feed_port=9912)
        self.create_feed(state)
        state.process = Mock()
        state.process.poll.return_value = None

        def write_preview(cmd, **kwargs):
            location = next(part.removeprefix("location=") for part in cmd if part.startswith("location="))
            Path(location).write_bytes(b"\x00\x00\x00" * 640 * 360)
            return Mock(returncode=0, stdout=b"", stderr=b"")

        with patch("videosim.gui.subprocess.run", side_effect=write_preview) as run:
            body, content_type = preview_image(state)

        self.assertEqual(content_type, "image/bmp")
        self.assertTrue(body.startswith(b"BM"))
        cmd = run.call_args.args[0]
        self.assertIn("gst-launch-1.0", cmd)
        self.assertIn("videotestsrc", cmd)
        self.assertNotIn("srtsrc", cmd)
        self.assertIn("num-buffers=1", cmd)
        self.assertIn("filesink", cmd)

    def test_preview_image_returns_placeholder_when_not_video_available(self):
        state = GuiState(mode="audio_only")
        self.create_feed(state)
        body, content_type = preview_image(state)

        self.assertEqual(content_type, "image/svg+xml")
        self.assertIn(b"Feed Preview", body)

    def test_preview_image_can_target_non_selected_stream(self):
        state = GuiState(feed_port=9912, width=320, height=180, framerate=10)
        self.create_feed(state)
        state.create_stream(name="Selected", protocol="srt", mode="audio_only")
        state.streams["stream-1"].process = Mock()
        state.streams["stream-1"].process.poll.return_value = None

        def write_preview(cmd, **kwargs):
            location = next(part.removeprefix("location=") for part in cmd if part.startswith("location="))
            Path(location).write_bytes(b"\x00\x00\x00" * 320 * 180)
            return Mock(returncode=0, stdout=b"", stderr=b"")

        with patch("videosim.gui.subprocess.run", side_effect=write_preview) as run:
            body, content_type = preview_image(state, "stream-1")

        self.assertEqual(content_type, "image/bmp")
        self.assertTrue(body.startswith(b"BM"))
        self.assertIn("video/x-raw,width=320,height=180,framerate=10/1", run.call_args.args[0])

    def test_rgb_frame_to_bmp_builds_browser_image(self):
        bmp = rgb_frame_to_bmp(bytes([255, 0, 0, 0, 255, 0]), 2, 1)

        self.assertTrue(bmp.startswith(b"BM"))
        self.assertEqual(int.from_bytes(bmp[18:22], "little"), 2)
        self.assertEqual(int.from_bytes(bmp[22:26], "little"), 1)

    def test_start_launches_normal_profile_feed(self):
        state = GuiState(feed_port=9912, width=320, height=180, framerate=10)
        self.create_feed(state)

        with patch("videosim.gui.subprocess.Popen") as popen:
            popen.return_value.stdout = []
            popen.return_value.pid = 1234
            popen.return_value.poll.return_value = None
            state.start()

        cmd = popen.call_args.args[0]
        self.assertIn("start", cmd)
        self.assertIn("--profile", cmd)
        self.assertIn("profiles/srt-normal.yaml", cmd)
        self.assertIn("--port", cmd)
        self.assertIn("9912", cmd)
        self.assertEqual(state.status, "running")
        self.assertTrue(any("Starting srt normal feed: profile=profiles/srt-normal.yaml" in line for line in state.logs))
        self.assertIn("Started srt normal feed at srt://127.0.0.1:9912?mode=caller pid=1234", state.logs)

    def test_start_launches_dash_profile_feed(self):
        state = GuiState(protocol="dash", http_port=18100, feed_port=9912, width=320, height=180, framerate=10)
        self.create_feed(state)

        with patch("videosim.gui.subprocess.Popen") as popen:
            popen.return_value.stdout = []
            popen.return_value.pid = 1234
            popen.return_value.poll.return_value = None
            state.start()

        cmd = popen.call_args.args[0]
        self.assertIn("profiles/dash-normal.yaml", cmd)
        self.assertIn("--protocol", cmd)
        self.assertIn("dash", cmd)
        self.assertIn("--dash-dir", cmd)
        self.assertIn("--dash-base-url", cmd)
        self.assertEqual(state.endpoint, "http://127.0.0.1:18100/dash/stream-1/manifest.mpd")
        self.assertIn("Started dash normal feed at http://127.0.0.1:18100/dash/stream-1/manifest.mpd pid=1234", state.logs)

    def test_verbose_gui_logs_feed_launch_command(self):
        state = GuiState(feed_port=9912)
        self.create_feed(state)

        with patch.dict("videosim.gui.os.environ", {"VIDEOSIM_VERBOSE": "1"}), patch(
            "videosim.gui.subprocess.Popen"
        ) as popen:
            popen.return_value.stdout = []
            popen.return_value.pid = 1234
            popen.return_value.poll.return_value = None
            state.start()

        self.assertTrue(any("Feed launch command:" in line for line in state.logs))

    def test_start_launches_selected_outage_profile(self):
        state = GuiState(feed_port=9912, mode="black_video")
        self.create_feed(state)

        with patch("videosim.gui.subprocess.Popen") as popen:
            popen.return_value.stdout = []
            popen.return_value.poll.return_value = None
            state.start()

        cmd = popen.call_args.args[0]
        self.assertIn("profiles/srt-black-video.yaml", cmd)

    def test_profile_for_returns_protocol_specific_profile(self):
        self.assertEqual(profile_for("srt", "black_video"), "profiles/srt-black-video.yaml")
        self.assertEqual(profile_for("dash", "black_video"), "profiles/dash-black-video.yaml")

    def test_unsupported_mode_does_not_start_feed(self):
        state = GuiState(mode="unknown")
        state.create_stream(name="Bad", protocol="srt", mode="normal")
        state.active_stream.mode = "unknown"
        state._sync_from_active()

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
        self.create_feed(state)

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
            state.apply_mode("no_captions", "dash")

        self.assertEqual(state.mode, "no_captions")
        self.assertEqual(state.protocol, "dash")
        self.assertIn("Restarting feed for dash no_captions mode", state.logs)
        killpg.assert_called()
        cmd = popen.call_args.args[0]
        self.assertIn("profiles/dash-no-captions.yaml", cmd)

    def test_start_failure_leaves_feed_stopped_with_error_log(self):
        state = GuiState()
        self.create_feed(state)

        with patch("videosim.gui.subprocess.Popen", side_effect=OSError("missing gst")):
            started = state.apply_mode("normal")

        self.assertFalse(started)
        self.assertEqual(state.status, "stopped")
        self.assertEqual(state.last_error, "Failed to start normal feed: missing gst")
        self.assertIn("Failed to start normal feed: missing gst", state.logs)

    def test_intentional_outage_is_visible_without_error(self):
        state = GuiState(mode="audio_only")
        self.create_feed(state)

        page = render_page(state)

        self.assertIn("Intentional outage: <strong>yes</strong>", page)
        self.assertIn("Last error: <strong>none</strong>", page)

    def test_pipeline_exit_sets_last_error_and_keeps_logs(self):
        state = GuiState()
        self.create_feed(state)
        process = Mock()
        process.stdout = ["pipeline failed"]
        process.poll.return_value = 2

        state._capture_logs(process)

        self.assertEqual(state.last_error, "Feed process exited with code 2: pipeline failed")
        self.assertIn("pipeline failed", state.logs)

    def test_validation_output_visible_in_gui(self):
        state = GuiState(feed_port=9912, width=320, height=180, framerate=10)
        self.create_feed(state)
        result = Mock(returncode=0, stdout="Validation PASS\nreachable=True\n")

        with patch("videosim.gui.subprocess.run", return_value=result) as run:
            passed = state.validate()

        self.assertTrue(passed)
        self.assertIn("Validation PASS", render_page(state))
        cmd = run.call_args.args[0]
        self.assertIn("validate", cmd)
        self.assertIn("profiles/srt-normal.yaml", cmd)
        self.assertIn("9912", cmd)

    def test_dash_validation_command_uses_dash_profile_and_directory(self):
        state = GuiState(protocol="dash", http_port=18100, dash_dir="/tmp/gui-dash")
        self.create_feed(state)
        result = Mock(returncode=0, stdout="Validation PASS\nreachable=True\n")

        with patch("videosim.gui.subprocess.run", return_value=result) as run:
            passed = state.validate()

        self.assertTrue(passed)
        cmd = run.call_args.args[0]
        self.assertIn("profiles/dash-normal.yaml", cmd)
        self.assertIn("--protocol", cmd)
        self.assertIn("dash", cmd)
        self.assertIn("--dash-dir", cmd)
        self.assertIn("/tmp/gui-dash/stream-1", cmd)

    def test_validation_failure_sets_actionable_error(self):
        state = GuiState()
        self.create_feed(state)
        result = Mock(returncode=1, stdout="Validation FAIL\nerrors=feed unreachable\n")

        with patch("videosim.gui.subprocess.run", return_value=result):
            passed = state.validate()

        self.assertFalse(passed)
        self.assertEqual(state.last_error, "errors=feed unreachable")
        self.assertIn("Validation failed", state.logs)

    def test_diagnostics_export_contains_state_validation_and_logs(self):
        state = GuiState(feed_port=9912, mode="black_video")
        self.create_feed(state)
        state.validation_output = "Validation PASS"
        state.log("Started black_video feed")

        diagnostics = diagnostics_text(state)

        self.assertIn("status=stopped", diagnostics)
        self.assertIn("protocol=srt", diagnostics)
        self.assertIn("mode=black_video", diagnostics)
        self.assertIn("intentional_outage=yes", diagnostics)
        self.assertIn("Validation PASS", diagnostics)
        self.assertIn("Started black_video feed", diagnostics)


if __name__ == "__main__":
    unittest.main()
