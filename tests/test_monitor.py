import unittest

from videosim.monitor import apply_issues, empty_monitor_state, issue, run_monitor_once
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


if __name__ == "__main__":
    unittest.main()
