import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from videosim.feed import VideoFeedConfig
from videosim.framerate import FrameRateReport
from videosim.loudness import LoudnessError, LoudnessReport
from videosim.monitor import (
    apply_issues,
    config_for_stream,
    empty_monitor_state,
    frame_rate_issues_for_stream,
    issue,
    loudness_issues_for_stream,
    run_monitor_once,
    tr101_issues_for_stream,
)
from videosim.tr101 import TR101_INDICATORS
from videosim.validator import ValidationReport
from tests.test_tr101 import (
    PAT_PID,
    PMT_PID,
    VIDEO_PID,
    packet,
    pat_section,
    pcr_adaptation,
    pes_packet,
    pmt_section,
    section_packet,
    si_section,
    valid_ts,
)


def stream(mode="normal"):
    return {
        "id": "stream-1",
        "name": "Primary",
        "protocol": "srt",
        "mode": mode,
        "status": "running",
        "endpoint": "srt://127.0.0.1:9000?mode=caller",
    }


def dash_issue_ids_for_segments(segments):
    with tempfile.TemporaryDirectory() as directory:
        for index, segment in enumerate(segments):
            Path(directory, f"video_0_{index}.ts").write_bytes(segment)
        config = VideoFeedConfig(protocol="dash", dash_dir=directory)
        return {item.monitor_id for item in tr101_issues_for_stream(stream() | {"protocol": "dash"}, config)}


def corrupt(section):
    broken = bytearray(section)
    broken[-1] ^= 0xFF
    return bytes(broken)


def repeated_with_nulls(*packets):
    return b"".join([*packets, *(packet(0x1FFF, b"", cc=index % 16) for index in range(100))])


class MonitorTest(unittest.TestCase):
    def test_monitor_catalogue_exposes_tr101_priority_3_status(self):
        monitors = {item["id"]: item for item in empty_monitor_state()["monitors"]}

        self.assertTrue(monitors["tr101_3_4_unreferenced_pid"]["implemented"])
        self.assertTrue(monitors["tr101_3_8_tdt_error"]["implemented"])
        self.assertTrue(monitors["tr101_3_3_buffer_error"]["implemented"])
        self.assertTrue(monitors["tr101_3_9_empty_buffer_error"]["implemented"])
        self.assertTrue(monitors["tr101_3_10_data_delay_error"]["implemented"])
        self.assertTrue(monitors["loudness_bs1770_measurement"]["implemented"])
        self.assertTrue(monitors["loudness_ebu_r128_integrated"]["implemented"])
        self.assertTrue(monitors["loudness_ebu_r128_true_peak"]["implemented"])
        self.assertTrue(monitors["loudness_atsc_a85_integrated"]["implemented"])
        self.assertTrue(monitors["video_frame_rate_match"]["implemented"])

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
            loudness_checker=lambda stream, config: [],
            frame_rate_checker=lambda stream, config: [],
        )

        self.assertEqual(reports[0].endpoint, "srt://app:9000?mode=caller")
        self.assertEqual({alarm["monitorId"] for alarm in state["alarms"]}, {"essence_video_present", "essence_audio_present"})

    def test_stream_alert_profile_filters_disabled_monitors(self):
        def validator(config):
            return ValidationReport(endpoint=config.endpoint, reachable=True, video_present=False, audio_present=False, captions_present=True)

        state = run_monitor_once(
            {"streams": [stream() | {"alertProfile": {"enabledMonitorIds": ["essence_video_present"], "delaySeconds": 0}}]},
            empty_monitor_state(),
            now=100,
            repeat_seconds=5,
            history_limit=20,
            srt_host="app",
            validator=validator,
            tr101_checker=lambda stream, config: [],
            loudness_checker=lambda stream, config: [],
            frame_rate_checker=lambda stream, config: [],
        )

        self.assertEqual({alarm["monitorId"] for alarm in state["alarms"]}, {"essence_video_present"})

    def test_audio_only_does_not_alert_on_missing_video_by_default(self):
        def validator(config):
            return ValidationReport(endpoint=config.endpoint, reachable=True, video_present=False, audio_present=True, captions_present=False)

        state = run_monitor_once(
            {"streams": [stream("audio_only")]},
            empty_monitor_state(),
            now=100,
            repeat_seconds=5,
            history_limit=20,
            srt_host="app",
            validator=validator,
            tr101_checker=lambda stream, config: [],
            loudness_checker=lambda stream, config: [],
            frame_rate_checker=lambda stream, config: [],
        )

        self.assertEqual(state["alarms"], [])

    def test_explicit_video_present_alert_overrides_audio_only_mode(self):
        def validator(config):
            return ValidationReport(endpoint=config.endpoint, reachable=True, video_present=False, audio_present=True, captions_present=False)

        state = run_monitor_once(
            {"streams": [stream("audio_only") | {"alertProfile": {"enabledMonitorIds": ["essence_video_present"], "delaySeconds": 0}}]},
            empty_monitor_state(),
            now=100,
            repeat_seconds=5,
            history_limit=20,
            srt_host="app",
            validator=validator,
            tr101_checker=lambda stream, config: [],
            loudness_checker=lambda stream, config: [],
            frame_rate_checker=lambda stream, config: [],
        )

        self.assertEqual([alarm["monitorId"] for alarm in state["alarms"]], ["essence_video_present"])

    def test_stream_alert_profile_disables_and_clears_active_alarm(self):
        def validator(config):
            return ValidationReport(endpoint=config.endpoint, reachable=True, video_present=False, audio_present=True, captions_present=True)

        state = run_monitor_once(
            {"streams": [stream() | {"alertProfile": {"enabledMonitorIds": ["essence_video_present"], "delaySeconds": 0}}]},
            empty_monitor_state(),
            now=100,
            repeat_seconds=5,
            history_limit=20,
            srt_host="app",
            validator=validator,
            tr101_checker=lambda stream, config: [],
            loudness_checker=lambda stream, config: [],
            frame_rate_checker=lambda stream, config: [],
        )
        state = run_monitor_once(
            {"streams": [stream() | {"alertProfile": {"enabledMonitorIds": [], "delaySeconds": 0}}]},
            state,
            now=105,
            repeat_seconds=5,
            history_limit=20,
            srt_host="app",
            validator=validator,
            tr101_checker=lambda stream, config: [],
            loudness_checker=lambda stream, config: [],
            frame_rate_checker=lambda stream, config: [],
        )

        self.assertFalse(state["alarms"][0]["active"])
        self.assertEqual(state["events"][-1]["type"], "alarm_cleared")

    def test_stream_alert_profile_delays_alarm_until_issue_persists(self):
        def validator(config):
            return ValidationReport(endpoint=config.endpoint, reachable=True, video_present=False, audio_present=True, captions_present=True)

        gui_state = {"streams": [stream() | {"alertProfile": {"enabledMonitorIds": ["essence_video_present"], "delaySeconds": 10}}]}
        state = empty_monitor_state()
        for now in (100, 109):
            state = run_monitor_once(
                gui_state,
                state,
                now=now,
                repeat_seconds=5,
                history_limit=20,
                srt_host="app",
                validator=validator,
                tr101_checker=lambda stream, config: [],
                loudness_checker=lambda stream, config: [],
                frame_rate_checker=lambda stream, config: [],
            )
            self.assertEqual(state["alarms"], [])
            self.assertEqual(state["pending"][0]["id"], "stream-1:essence_video_present")

        state = run_monitor_once(
            gui_state,
            state,
            now=110,
            repeat_seconds=5,
            history_limit=20,
            srt_host="app",
            validator=validator,
            tr101_checker=lambda stream, config: [],
            loudness_checker=lambda stream, config: [],
            frame_rate_checker=lambda stream, config: [],
        )

        self.assertEqual(state["alarms"][0]["monitorId"], "essence_video_present")
        self.assertEqual(state["events"][0]["type"], "alarm_raised")

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
            loudness_checker=lambda stream, config: [],
            frame_rate_checker=lambda stream, config: [],
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
                loudness_checker=lambda stream, config: [],
                frame_rate_checker=lambda stream, config: [],
            )

        self.assertIn("tr101_1_2_sync_byte_error", {alarm["monitorId"] for alarm in state["alarms"]})
        self.assertEqual(state["events"][0]["type"], "alarm_raised")

    def test_config_for_stream_keeps_selected_frame_rate(self):
        config = config_for_stream(stream() | {"framerate": "59.94"}, "app")

        self.assertEqual(config.framerate, "59.94")
        self.assertEqual(config.endpoint, "srt://app:9000?mode=caller")

    def test_config_for_external_stream_preserves_registered_endpoint(self):
        config = config_for_stream(
            stream()
            | {
                "source": "external",
                "endpoint": "srt://camera.local:9999?mode=caller",
                "framerate": "59.94",
            },
            "app",
        )

        self.assertEqual(config.framerate, "59.94")
        self.assertEqual(config.endpoint, "srt://camera.local:9999?mode=caller")
        self.assertTrue(config.passive)

    def test_config_for_worker_dash_stream_uses_master_monitor_endpoint(self):
        config = config_for_stream(
            stream()
            | {
                "protocol": "dash",
                "endpoint": "http://127.0.0.1:8080/dash/stream-1/manifest.mpd",
                "monitorEndpoint": "http://master:8080/dash/stream-1/manifest.mpd",
            },
            "app",
        )

        self.assertEqual(config.protocol, "dash")
        self.assertEqual(config.endpoint, "http://master:8080/dash/stream-1/manifest.mpd")

    def test_monitor_once_ignores_external_missing_video_until_alert_selected(self):
        def validator(config):
            return ValidationReport(endpoint=config.endpoint, reachable=True, video_present=False, audio_present=True, captions_present=True)

        state = run_monitor_once(
            {"streams": [stream() | {"source": "external", "endpoint": "srt://camera.local:9999?mode=caller"}]},
            empty_monitor_state(),
            now=100,
            repeat_seconds=5,
            history_limit=20,
            srt_host="app",
            validator=validator,
            tr101_checker=lambda stream, config: [],
            loudness_checker=lambda stream, config: [],
            frame_rate_checker=lambda stream, config: [],
        )

        self.assertEqual(state["alarms"], [])

        state = run_monitor_once(
            {
                "streams": [
                    stream()
                    | {
                        "source": "external",
                        "endpoint": "srt://camera.local:9999?mode=caller",
                        "alertProfile": {"enabledMonitorIds": ["essence_video_present"], "delaySeconds": 0},
                    }
                ]
            },
            empty_monitor_state(),
            now=100,
            repeat_seconds=5,
            history_limit=20,
            srt_host="app",
            validator=validator,
            tr101_checker=lambda stream, config: [],
            loudness_checker=lambda stream, config: [],
            frame_rate_checker=lambda stream, config: [],
        )

        self.assertEqual([alarm["monitorId"] for alarm in state["alarms"]], ["essence_video_present"])

    def test_frame_rate_checker_raises_mismatch_alarm(self):
        with patch("videosim.monitor.measure_frame_rate", return_value=FrameRateReport(measured_fps=60.0, source="sample")):
            issues = frame_rate_issues_for_stream(stream() | {"framerate": "50"}, VideoFeedConfig(framerate="50"))

        self.assertEqual([item.monitor_id for item in issues], ["video_frame_rate_match"])
        self.assertIn("60.00 fps", issues[0].message)
        self.assertIn("50.00 fps", issues[0].message)

    def test_frame_rate_checker_skips_matching_and_audio_only(self):
        with patch("videosim.monitor.measure_frame_rate", return_value=FrameRateReport(measured_fps=59.94, source="sample")) as checker:
            self.assertEqual(frame_rate_issues_for_stream(stream() | {"framerate": "59.94"}, VideoFeedConfig(framerate="59.94")), [])

        checker.assert_called_once()
        with patch("videosim.monitor.measure_frame_rate") as checker:
            self.assertEqual(frame_rate_issues_for_stream(stream("audio_only"), VideoFeedConfig(video=False, captions=False)), [])
        checker.assert_not_called()

    def test_loudness_checker_raises_ebu_and_atsc_alarms(self):
        loud_stream = stream()
        config = VideoFeedConfig()

        def checker(config, sample_seconds=5.0):
            return LoudnessReport(integrated_lufs=-20.0, true_peak_dbtp=-0.5, source=config.endpoint)

        with patch("videosim.monitor.measure_loudness", checker):
            issues = loudness_issues_for_stream(loud_stream, config)

        self.assertEqual(
            {item.monitor_id for item in issues},
            {
                "loudness_ebu_r128_integrated",
                "loudness_ebu_r128_true_peak",
                "loudness_atsc_a85_integrated",
            },
        )

    def test_loudness_checker_skips_audio_absent_modes(self):
        with patch("videosim.monitor.measure_loudness") as checker:
            issues = loudness_issues_for_stream(stream("video_only"), VideoFeedConfig(audio=False))

        self.assertEqual(issues, [])
        checker.assert_not_called()

    def test_loudness_checker_raises_measurement_alarm_on_probe_failure(self):
        with patch("videosim.monitor.measure_loudness", side_effect=LoudnessError("no audio samples")):
            issues = loudness_issues_for_stream(stream(), VideoFeedConfig())

        self.assertEqual([item.monitor_id for item in issues], ["loudness_bs1770_measurement"])
        self.assertIn("no audio samples", issues[0].message)

    def test_monitor_once_adds_loudness_alarm_issues(self):
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
            tr101_checker=lambda current, config: [],
            loudness_checker=lambda current, config: [issue(current, "loudness_ebu_r128_integrated", "too loud")],
            frame_rate_checker=lambda stream, config: [],
        )

        self.assertEqual(state["alarms"][0]["monitorId"], "loudness_ebu_r128_integrated")
        self.assertEqual(state["events"][0]["type"], "alarm_raised")

    def test_dash_tr101_fixture_samples_cover_every_indicator(self):
        fixtures = [
            [b"\x00" * 376],
            [section_packet(PAT_PID, si_section(0x40))],
            [section_packet(PAT_PID, pat_section())],
            [
                b"".join(
                    [
                        section_packet(PAT_PID, pat_section()),
                        section_packet(PMT_PID, pmt_section()),
                        packet(VIDEO_PID, b"video", cc=0, adaptation=pcr_adaptation(0.00)),
                        packet(VIDEO_PID, b"video", cc=0),
                        packet(VIDEO_PID, b"video", cc=0),
                        packet(VIDEO_PID, b"video", cc=1, adaptation=pcr_adaptation(0.20)),
                        pes_packet(VIDEO_PID, 0.00, cc=2),
                        pes_packet(VIDEO_PID, 0.80, cc=3),
                    ]
                )
            ],
            [
                b"".join(
                    [
                        section_packet(PAT_PID, pat_section()),
                        section_packet(PMT_PID, pmt_section()),
                        packet(VIDEO_PID, b"video", cc=0, adaptation=pcr_adaptation(0.00)),
                    ]
                )
            ],
            [section_packet(PAT_PID, corrupt(pat_section()), transport_error=True)],
            [b"".join([section_packet(PAT_PID, pat_section()), section_packet(PMT_PID, pmt_section()), packet(VIDEO_PID, b"scrambled", scrambled=True)])],
            [b"".join([valid_ts(), packet(300, b"private")])],
            [section_packet(0x0010, si_section(0x42))],
            [section_packet(0x0010, si_section(0x41)), *([valid_ts()] * 4)],
            [repeated_with_nulls(section_packet(0x0013, si_section(0x71), cc=0), section_packet(0x0013, si_section(0x71), cc=1))],
            [section_packet(0x0011, si_section(0x40))],
            [section_packet(0x0011, si_section(0x46)), *([valid_ts()] * 4)],
            [section_packet(0x0012, si_section(0x40))],
            [b"".join([section_packet(0x0012, si_section(0x4F, section_number=0)), section_packet(0x0012, si_section(0x4F, section_number=1), cc=1)]), *([valid_ts()] * 4)],
            [section_packet(0x0012, si_section(0x4E, section_number=0))],
            [section_packet(0x0013, si_section(0x40))],
            [section_packet(0x0014, si_section(0x40))],
            [
                b"".join(
                    [
                        section_packet(PAT_PID, pat_section(), cc=0),
                        section_packet(PMT_PID, pmt_section(), cc=0),
                        packet(VIDEO_PID, b"video", cc=0, adaptation=pcr_adaptation(0.00)),
                        pes_packet(VIDEO_PID, 3.20, cc=1),
                        pes_packet(VIDEO_PID, 3.40, cc=2),
                    ]
                )
            ],
        ]

        covered = set()
        for segments in fixtures:
            covered.update(dash_issue_ids_for_segments(segments))

        self.assertEqual(set(TR101_INDICATORS) - covered, set())


if __name__ == "__main__":
    unittest.main()
