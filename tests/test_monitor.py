import tempfile
import unittest
from pathlib import Path

from videosim.feed import VideoFeedConfig
from videosim.monitor import apply_issues, empty_monitor_state, issue, run_monitor_once, tr101_issues_for_stream
from videosim.validator import ValidationReport


def stream(mode="normal"):
    return {
        "id": "stream-1",
        "name": "Primary",
        "protocol": "srt",
        "mode": mode,
        "status": "running",
        "endpoint": "srt://127.0.0.1:9000?mode=caller",
    }


class MonitorTest(unittest.TestCase):
    def test_monitor_catalogue_exposes_tr101_priority_3_status(self):
        monitors = {item["id"]: item for item in empty_monitor_state()["monitors"]}

        self.assertTrue(monitors["tr101_3_4_unreferenced_pid"]["implemented"])
        self.assertTrue(monitors["tr101_3_8_tdt_error"]["implemented"])
        self.assertFalse(monitors["tr101_3_3_buffer_error"]["implemented"])
        self.assertFalse(monitors["tr101_3_9_empty_buffer_error"]["implemented"])
        self.assertFalse(monitors["tr101_3_10_data_delay_error"]["implemented"])

    def test_alarm_events_repeat_every_five_seconds_and_clear_to_steady(self):
        item = issue(stream(), "essence_video_present", "Expected video is absent")
        state = empty_monitor_state()

        state = apply_issues(state, [item], now=100, repeat_seconds=5, history_limit=20)
        state = apply_issues(state, [item], now=104, repeat_seconds=5, history_limit=20)
        state = apply_issues(state, [item], now=105, repeat_seconds=5, history_limit=20)
        state = apply_issues(state, [], now=106, repeat_seconds=5, history_limit=20)

        self.assertEqual([event["type"] for event in state["events"]], ["alarm_raised", "alarm_active", "alarm_cleared"])
        self.assertFalse(state["alarms"][0]["active"])
        self.assertEqual(state["alarms"][0]["status"], "steady")

        state = apply_issues(state, [item], now=111, repeat_seconds=5, history_limit=20)

        self.assertEqual(state["events"][-1]["type"], "alarm_raised")
        self.assertTrue(state["alarms"][0]["active"])

    def test_monitor_once_detects_missing_video_and_audio(self):
        reports = []

        def validator(config):
            reports.append(config)
            return ValidationReport(
                endpoint=config.endpoint,
                reachable=True,
                video_present=False,
                audio_present=False,
                captions_present=True,
            )

        state = run_monitor_once(
            {"streams": [stream()]},
            empty_monitor_state(),
            now=100,
            repeat_seconds=5,
            history_limit=20,
            srt_host="app",
            validator=validator,
            tr101_checker=lambda stream, config: [],
        )

        self.assertEqual(reports[0].endpoint, "srt://app:9000?mode=caller")
        self.assertEqual({alarm["monitorId"] for alarm in state["alarms"]}, {"essence_video_present", "essence_audio_present"})

    def test_monitor_once_adds_tr101_alarm_issues(self):
        def validator(config):
            return ValidationReport(
                endpoint=config.endpoint,
                reachable=True,
                video_present=True,
                audio_present=True,
                captions_present=True,
            )

        state = run_monitor_once(
            {"streams": [stream()]},
            empty_monitor_state(),
            now=100,
            repeat_seconds=5,
            history_limit=20,
            srt_host="app",
            validator=validator,
            tr101_checker=lambda current, config: [issue(current, "tr101_1_4_continuity_count_error", "PID 200 continuity counter jumped")],
        )

        self.assertEqual(state["alarms"][0]["monitorId"], "tr101_1_4_continuity_count_error")
        self.assertEqual(state["events"][0]["type"], "alarm_raised")

    def test_monitor_samples_dash_ts_segments_for_tr101_alarms(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "video_0_1.ts").write_bytes(b"\x00" * 188)
            config = VideoFeedConfig(protocol="dash", dash_dir=directory)

            issues = tr101_issues_for_stream(stream() | {"protocol": "dash"}, config)

        self.assertIn("tr101_1_2_sync_byte_error", {item.monitor_id for item in issues})

    def test_monitor_once_samples_dash_segments_into_tr101_alarm_state(self):
        dash_root = Path("/tmp/videosim-dash")
        dash_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="stream-", dir=dash_root) as directory:
            stream_id = Path(directory).name
            Path(directory, "video_0_1.ts").write_bytes(b"\x00" * 188)
            dash_stream = stream() | {
                "id": stream_id,
                "protocol": "dash",
                "endpoint": f"http://127.0.0.1:8080/dash/{stream_id}/manifest.mpd",
            }

            def validator(config):
                self.assertEqual(config.dash_dir, directory)
                return ValidationReport(
                    endpoint=config.endpoint,
                    reachable=True,
                    video_present=True,
                    audio_present=True,
                    captions_present=True,
                )

            state = run_monitor_once(
                {"streams": [dash_stream]},
                empty_monitor_state(),
                now=100,
                repeat_seconds=5,
                history_limit=20,
                srt_host="app",
                validator=validator,
            )

        self.assertIn("tr101_1_2_sync_byte_error", {alarm["monitorId"] for alarm in state["alarms"]})
        self.assertEqual(state["events"][0]["type"], "alarm_raised")


if __name__ == "__main__":
    unittest.main()
