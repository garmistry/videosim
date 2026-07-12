from collections import Counter
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

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
    next_deep_check_at,
    run_monitor_once,
    srt_ts_sample,
    tr101_issues_for_stream,
)
from videosim.probe_deadline import use_probe_deadline
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
    def test_1000_stream_deep_check_offsets_cover_the_cadence(self):
        bins = Counter(
            int((next_deep_check_at(f"stream-{index}", 100, 60) - 100) // 5)
            for index in range(1000)
        )

        self.assertEqual(set(bins), set(range(12)))
        self.assertLessEqual(max(bins.values()), 125)

    def test_stream_budget_cancels_srt_transport_sample(self):
        process = Mock()
        process.communicate.side_effect = [
            subprocess.TimeoutExpired("gst-launch-1.0", 1),
            (b"", b""),
        ]
        with use_probe_deadline(time.monotonic() + 1), patch(
            "videosim.monitor.subprocess.Popen",
            return_value=process,
        ):
            with self.assertRaisesRegex(TimeoutError, "stream probe budget exhausted"):
                srt_ts_sample(VideoFeedConfig(), sample_seconds=5)

        self.assertLess(process.communicate.call_args_list[0].kwargs["timeout"], 1)
        process.terminate.assert_called_once_with()

    def test_stream_budget_defers_remaining_checks_without_clearing_alarm(self):
        current_stream = stream()
        previous = apply_issues(
            empty_monitor_state(),
            [issue(current_stream, "essence_video_present", "missing video")],
            now=90,
            repeat_seconds=5,
            history_limit=20,
        )
        clock = [0.0]

        def validator(config):
            clock[0] = 2.0
            return ValidationReport(
                endpoint=config.endpoint,
                reachable=True,
                video_present=True,
                audio_present=True,
                captions_present=True,
            )

        def should_not_run(*_args):
            raise AssertionError("lower-priority check ran after budget exhaustion")

        result = run_monitor_once(
            {"streams": [current_stream]},
            previous,
            now=100,
            repeat_seconds=5,
            history_limit=20,
            srt_host="app",
            validator=validator,
            tr101_checker=should_not_run,
            loudness_checker=should_not_run,
            frame_rate_checker=should_not_run,
            monotonic=lambda: clock[0],
            stream_budget_seconds=1,
        )

        self.assertEqual(
            [(alarm["id"], alarm["active"]) for alarm in result["alarms"]],
            [("stream-1:essence_video_present", True)],
        )
        self.assertEqual([event["type"] for event in result["events"]], ["alarm_raised"])
        self.assertEqual(result["probeMetrics"]["outcomes"], {"timeout": 4})
        self.assertEqual(
            {item["status"] for item in result["monitorObservations"]},
            {"timeout"},
        )

    def test_monitor_once_runs_streams_concurrently_when_bounded_pool_is_enabled(self):
        entered = 0
        entered_lock = threading.Lock()
        both_entered = threading.Event()

        def validator(config):
            nonlocal entered
            with entered_lock:
                entered += 1
                if entered == 2:
                    both_entered.set()
            if not both_entered.wait(timeout=2):
                raise AssertionError("stream checks did not overlap")
            return ValidationReport(
                endpoint=config.endpoint,
                reachable=True,
                video_present=False,
                audio_present=False,
                captions_present=True,
            )

        streams = [stream() | {"id": f"stream-{index}"} for index in (1, 2)]
        result = run_monitor_once(
            {"streams": streams},
            empty_monitor_state(),
            now=100,
            repeat_seconds=5,
            history_limit=20,
            srt_host="app",
            validator=validator,
            tr101_checker=lambda current, config: [],
            max_concurrency=2,
        )

        self.assertEqual(entered, 2)
        self.assertEqual(result["probeMetrics"]["streamCount"], 2)
        self.assertEqual(
            {item["streamId"] for item in result["monitorObservations"]},
            {"stream-1", "stream-2"},
        )

    def test_concurrent_monitor_validates_every_stream_before_deep_checks(self):
        validated_ports = set()
        lock = threading.Lock()

        def validator(config):
            with lock:
                validated_ports.add(config.port)
            return ValidationReport(
                endpoint=config.endpoint,
                reachable=True,
                video_present=True,
                audio_present=True,
                captions_present=True,
            )

        def deep_checker(_stream, _config):
            with lock:
                self.assertEqual(validated_ports, {9001, 9002, 9003})
            return []

        streams = [
            stream()
            | {
                "id": f"stream-{index}",
                "endpoint": f"srt://127.0.0.1:{9000 + index}?mode=caller",
            }
            for index in (1, 2, 3)
        ]
        result = run_monitor_once(
            {"streams": streams},
            empty_monitor_state(),
            now=100,
            repeat_seconds=5,
            history_limit=20,
            srt_host="app",
            validator=validator,
            tr101_checker=deep_checker,
            loudness_checker=deep_checker,
            frame_rate_checker=deep_checker,
            max_concurrency=2,
        )

        metrics = result["probeMetrics"]["streams"]
        self.assertEqual([item["check"] for item in metrics[:3]], ["validation"] * 3)
        self.assertEqual(result["probeMetrics"]["streamCount"], 3)
        self.assertEqual(result["probeMetrics"]["checkCount"], 12)

    def test_concurrent_deep_checks_use_staggered_cadence_without_false_clear(self):
        streams = [
            stream()
            | {
                "id": f"stream-{index}",
                "endpoint": f"srt://127.0.0.1:{9000 + index}?mode=caller",
            }
            for index in (1, 2)
        ]
        validation_calls = []
        tr101_calls = []

        def validator(config):
            validation_calls.append(config.port)
            return ValidationReport(
                endpoint=config.endpoint,
                reachable=True,
                video_present=True,
                audio_present=True,
                captions_present=True,
            )

        def tr101_checker(current, _config):
            tr101_calls.append(current["id"])
            return [issue(current, "tr101_1_1_ts_sync_loss", "sync lost")]

        def monitor(state, now):
            return run_monitor_once(
                {"streams": streams},
                state,
                now=now,
                repeat_seconds=5,
                history_limit=20,
                srt_host="app",
                validator=validator,
                tr101_checker=tr101_checker,
                frame_rate_checker=lambda *_args: [],
                loudness_checker=lambda *_args: [],
                max_concurrency=2,
                deep_check_interval_seconds=60,
            )

        first = monitor(empty_monitor_state(), 100)
        due = first["deepCheckSchedule"]
        second = monitor(first, min(due.values()) - 0.001)

        self.assertEqual(len(set(due.values())), 2)
        self.assertEqual(first["probeMetrics"]["checkCount"], 8)
        self.assertEqual(len(validation_calls), 4)
        self.assertEqual(len(tr101_calls), 2)
        self.assertEqual(
            second["probeMetrics"]["outcomes"], {"success": 2, "skipped": 2}
        )
        self.assertEqual(len(second["alarms"]), 2)
        self.assertTrue(all(alarm["active"] for alarm in second["alarms"]))

        monitor(second, min(due.values()))
        self.assertEqual(len(tr101_calls), 3)

    def test_deep_check_interval_rejects_negative_values(self):
        with self.assertRaisesRegex(ValueError, "deep_check_interval_seconds"):
            run_monitor_once(
                {"streams": []},
                empty_monitor_state(),
                now=100,
                repeat_seconds=5,
                history_limit=20,
                srt_host="app",
                deep_check_interval_seconds=-1,
            )

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
        observations = {
            item["monitorId"]: item["status"]
            for item in state["monitorObservations"]
        }
        self.assertEqual(observations["feed_reachable"], "healthy")
        self.assertEqual(observations["essence_video_present"], "unhealthy")
        self.assertEqual(observations["essence_audio_present"], "unhealthy")
        self.assertEqual(observations["essence_captions_present"], "healthy")

    def test_monitor_records_deterministic_probe_and_batch_metrics(self):
        ticks = iter([0, 1, 1.1, 2, 2.2, 3, 3.3, 4, 4.4, 5])

        state = run_monitor_once(
            {"streams": [stream()]},
            empty_monitor_state(),
            now=100,
            repeat_seconds=5,
            history_limit=20,
            srt_host="app",
            validator=lambda config: ValidationReport(
                endpoint=config.endpoint,
                reachable=True,
                video_present=True,
                audio_present=True,
                captions_present=True,
            ),
            tr101_checker=lambda stream, config: [],
            loudness_checker=lambda stream, config: [],
            frame_rate_checker=lambda stream, config: [],
            monotonic=lambda: next(ticks),
        )

        metrics = state["probeMetrics"]
        self.assertEqual(metrics["batchDurationMs"], 5000.0)
        self.assertEqual(metrics["streamCount"], 1)
        self.assertEqual(metrics["checkCount"], 4)
        self.assertEqual(metrics["outcomes"], {"success": 4})
        self.assertEqual(
            [(item["check"], item["durationMs"]) for item in metrics["streams"]],
            [("validation", 100.0), ("tr101", 200.0), ("frame_rate", 300.0), ("loudness", 400.0)],
        )

    def test_monitor_probe_metrics_classify_issue_timeout_and_skipped(self):
        ticks = iter([0, 1, 1.1, 2, 2.2, 3])

        state = run_monitor_once(
            {"streams": [stream()]},
            empty_monitor_state(),
            now=100,
            repeat_seconds=5,
            history_limit=20,
            srt_host="app",
            validator=lambda config: ValidationReport(endpoint=config.endpoint, reachable=False),
            tr101_checker=lambda stream, config: (_ for _ in ()).throw(TimeoutError("sample timed out")),
            loudness_checker=lambda stream, config: [],
            frame_rate_checker=lambda stream, config: [],
            monotonic=lambda: next(ticks),
        )

        metrics = {item["check"]: item for item in state["probeMetrics"]["streams"]}
        self.assertEqual(metrics["validation"]["outcome"], "issue")
        self.assertEqual(metrics["tr101"]["outcome"], "timeout")
        self.assertEqual(metrics["frame_rate"]["outcome"], "skipped")
        self.assertEqual(metrics["loudness"]["outcome"], "skipped")
        self.assertEqual(state["probeMetrics"]["outcomes"], {"issue": 1, "timeout": 1, "skipped": 2})
        observations = {
            item["monitorId"]: item["status"]
            for item in state["monitorObservations"]
        }
        self.assertEqual(observations["feed_reachable"], "unhealthy")
        self.assertEqual(observations["video_frame_rate_match"], "skipped")
        self.assertEqual(observations["loudness_bs1770_measurement"], "skipped")
        self.assertEqual(observations["tr101_1_1_ts_sync_loss"], "timeout")

    def test_monitor_probe_metrics_classify_checker_error(self):
        ticks = iter([0, 1, 1.1, 2, 2.1, 3, 3.2, 4])

        state = run_monitor_once(
            {"streams": [stream("video_only")]},
            empty_monitor_state(),
            now=100,
            repeat_seconds=5,
            history_limit=20,
            srt_host="app",
            validator=lambda config: ValidationReport(
                endpoint=config.endpoint,
                reachable=True,
                video_present=True,
                audio_present=False,
                captions_present=True,
            ),
            tr101_checker=lambda stream, config: [],
            loudness_checker=lambda stream, config: [],
            frame_rate_checker=lambda stream, config: (_ for _ in ()).throw(RuntimeError("ffprobe crashed")),
            monotonic=lambda: next(ticks),
        )

        frame_metric = next(item for item in state["probeMetrics"]["streams"] if item["check"] == "frame_rate")
        self.assertEqual(frame_metric["outcome"], "error")
        self.assertEqual(frame_metric["detail"], "ffprobe crashed")

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
