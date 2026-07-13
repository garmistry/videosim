import json
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from videosim.control_plane import WorkerReportConflict, WorkerReportPersistenceError, WorkerReportValidationError
from videosim.feed_store import SqliteFeedStore
from videosim.gui import (
    GuiState,
    MODE_CONTROLS,
    PROFILE_OPTIONS,
    PROTOCOL_OPTIONS,
    apply_worker_report,
    capacity_aware_assignments,
    clear_monitor_events,
    diagnostics_text,
    mode_from_controls,
    mode_from_form,
    normalize_worker_capacity,
    profile_for,
    preview_image,
    render_page,
    rgb_frame_to_bmp,
    state_payload,
    worker_assignments_payload,
)


def empty_worker_state():
    return {"updatedAt": "now", "alarms": [], "events": [], "pending": []}


class GuiTest(unittest.TestCase):
    def test_worker_capacity_rejects_invalid_probe_controls(self):
        for capacity in (
            {"maxConcurrentChecks": 0},
            {"maxConcurrentDeepChecks": 0},
            {"pressure": []},
            {"pressure": {"assignedStreams": -1}},
            {"pressure": {"cycleActive": 1}},
            {"pressure": {"lastBatchDurationMs": float("inf")}},
            {"pressure": {"unknown": 1}},
            {"streamBudgetSeconds": float("inf")},
            {"deepCheckIntervalSeconds": "60"},
            {"batchBudgetSeconds": 0},
            {"maxSrtStreams": 0},
            {"maxDashStreams": True},
        ):
            with self.subTest(capacity=capacity), self.assertRaises(
                WorkerReportValidationError
            ):
                normalize_worker_capacity(capacity)

    def test_capacity_aware_assignments_leave_over_capacity_streams_unassigned(self):
        state = GuiState()
        streams = [
            state.create_stream(
                name=f"Capacity stream {index}",
                source="external",
                external_url=f"srt://example.test:{9000 + index}?mode=caller",
                select=False,
            )
            for index in range(5)
        ]
        assignments, shortfall = capacity_aware_assignments(
            [
                {"id": "worker-a", "capacity": {"maxStreams": 2}},
                {"id": "worker-b", "capacity": {"maxStreams": 1}},
            ],
            streams,
        )
        self.assertEqual(
            [len(assignments[worker]) for worker in ("worker-a", "worker-b")],
            [2, 1],
        )
        self.assertEqual(shortfall, 2)

    def test_spool_blocked_workers_receive_no_assignments(self):
        state = GuiState()
        streams = [
            state.create_stream(
                name=f"Blocked worker stream {index}",
                source="external",
                external_url=f"srt://example.test:{9100 + index}?mode=caller",
                select=False,
            )
            for index in range(4)
        ]

        assignments, shortfall = capacity_aware_assignments(
            [
                {
                    "id": "worker-a",
                    "capacity": {"pressure": {"spoolBlocked": True}},
                },
                {"id": "worker-b", "capacity": {}},
            ],
            streams,
        )

        self.assertEqual(assignments["worker-a"], [])
        self.assertEqual(assignments["worker-b"], streams)
        self.assertEqual(shortfall, 0)

    def test_all_spool_blocked_workers_report_full_shortfall(self):
        state = GuiState()
        streams = [
            state.create_stream(
                name=f"Unavailable worker stream {index}",
                source="external",
                external_url=f"srt://example.test:{9200 + index}?mode=caller",
                select=False,
            )
            for index in range(3)
        ]

        assignments, shortfall = capacity_aware_assignments(
            [
                {
                    "id": "worker-a",
                    "capacity": {"pressure": {"spoolBlocked": True}},
                },
                {
                    "id": "worker-b",
                    "capacity": {
                        "maxStreams": 100,
                        "pressure": {"spoolBlocked": True},
                    },
                },
            ],
            streams,
        )

        self.assertEqual(assignments, {"worker-a": [], "worker-b": []})
        self.assertEqual(shortfall, len(streams))

    def test_protocol_capacity_tokens_prevent_mixed_over_admission(self):
        state = GuiState()
        streams = [
            state.create_stream(
                name=f"SRT {index}",
                protocol="srt",
                source="external",
                external_url=f"srt://example.test:{9000 + index}?mode=caller",
                select=False,
            )
            for index in range(5)
        ] + [
            state.create_stream(
                name=f"DASH {index}",
                protocol="dash",
                source="external",
                external_url=f"https://example.test/{index}/manifest.mpd",
                select=False,
            )
            for index in range(2)
        ]
        workers = [
            {
                "id": "worker-a",
                "capacity": {
                    "maxStreams": 4,
                    "maxSrtStreams": 1,
                    "maxDashStreams": 3,
                },
            },
            {
                "id": "worker-b",
                "capacity": {
                    "maxStreams": 4,
                    "maxSrtStreams": 3,
                    "maxDashStreams": 1,
                },
            },
        ]

        assignments, shortfall = capacity_aware_assignments(workers, streams)

        self.assertEqual(shortfall, 1)
        self.assertEqual(
            {
                worker_id: {
                    protocol: sum(
                        stream.protocol == protocol
                        for stream in assignments[worker_id]
                    )
                    for protocol in ("srt", "dash")
                }
                for worker_id in assignments
            },
            {
                "worker-a": {"srt": 1, "dash": 2},
                "worker-b": {"srt": 3, "dash": 0},
            },
        )

    def test_1000_mixed_streams_fill_protocol_tokens_evenly(self):
        state = GuiState()
        streams = [
            state.create_stream(
                name=f"SRT {index}",
                protocol="srt",
                source="external",
                external_url=f"srt://example.test:{9000 + index}?mode=caller",
                select=False,
            )
            for index in range(500)
        ] + [
            state.create_stream(
                name=f"DASH {index}",
                protocol="dash",
                source="external",
                external_url=f"https://example.test/{index}/manifest.mpd",
                select=False,
            )
            for index in range(500)
        ]
        workers = [
            {
                "id": f"worker-{index:02d}",
                "capacity": {
                    "maxStreams": 100,
                    "maxSrtStreams": 50,
                    "maxDashStreams": 50,
                },
            }
            for index in range(10)
        ]

        assignments, shortfall = capacity_aware_assignments(workers, streams)

        self.assertEqual(shortfall, 0)
        for assigned in assignments.values():
            self.assertEqual(len(assigned), 100)
            self.assertEqual(sum(stream.protocol == "srt" for stream in assigned), 50)
            self.assertEqual(sum(stream.protocol == "dash" for stream in assigned), 50)

    def test_1000_target_plus_30_percent_headroom_survives_one_blocked_worker(self):
        state = GuiState()
        streams = [
            state.create_stream(
                name=f"Headroom SRT {index}",
                protocol="srt",
                source="external",
                external_url=f"srt://example.test:{10000 + index}?mode=caller",
                select=False,
            )
            for index in range(650)
        ] + [
            state.create_stream(
                name=f"Headroom DASH {index}",
                protocol="dash",
                source="external",
                external_url=f"https://example.test/headroom/{index}/manifest.mpd",
                select=False,
            )
            for index in range(650)
        ]
        workers = [
            {
                "id": f"worker-{index:02d}",
                "capacity": {
                    "maxStreams": 145,
                    "maxSrtStreams": 73,
                    "maxDashStreams": 73,
                    "pressure": {"spoolBlocked": index == 0},
                },
            }
            for index in range(10)
        ]

        assignments, shortfall = capacity_aware_assignments(workers, streams)

        self.assertEqual(shortfall, 0)
        self.assertEqual(assignments["worker-00"], [])
        self.assertEqual(sum(map(len, assignments.values())), 1300)
        for worker_id, assigned in assignments.items():
            if worker_id == "worker-00":
                continue
            self.assertLessEqual(len(assigned), 145)
            self.assertLessEqual(
                sum(stream.protocol == "srt" for stream in assigned), 73
            )
            self.assertLessEqual(
                sum(stream.protocol == "dash" for stream in assigned), 73
            )

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
        self.assertFalse(payload["monitor"]["connected"])
        self.assertIn({"id": "feed_reachable", "name": "Feed reachable", "priority": "platform", "severity": "critical", "implemented": True, "description": "Input feed can be ingested."}, payload["alertOptions"])
        self.assertEqual(payload["streams"][0]["previewUrl"], "/feeds/stream-1/preview.jpg")
        self.assertFalse(payload["streams"][0]["previewAvailable"])

    def test_alert_profile_ui_exists_without_running_monitor(self):
        state = GuiState(feed_port=9912)
        self.create_feed(state)

        payload = state_payload(state)
        page = render_page(state)

        self.assertGreater(len(payload["alertOptions"]), 1)
        self.assertFalse(payload["monitor"]["connected"])
        self.assertIn('action="/streams/alerts"', page)
        self.assertIn('name="alert_action" value="enable_all"', page)
        self.assertIn('name="alert_action" value="disable_all"', page)
        self.assertIn('value="feed_reachable"', page)

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
                        "monitors": [
                            {
                                "id": "essence_video_present",
                                "name": "Video present",
                                "severity": "critical",
                                "priority": "platform",
                                "implemented": True,
                                "description": "Expected video essence is present.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state = GuiState(feed_port=9912, monitor_state_path=str(path))
            self.create_feed(state)

            payload = state_payload(state)
            page = render_page(state)

        self.assertEqual(payload["monitor"]["alarms"][0]["monitorName"], "Video present")
        self.assertEqual(payload["alertOptions"][0]["id"], "essence_video_present")
        self.assertIn("Monitor alarms (1 active)", page)
        self.assertIn("Expected video is absent", page)
        self.assertIn('action="/streams/alerts"', page)
        self.assertIn('name="alert_delay_seconds"', page)
        self.assertIn('value="essence_video_present"', page)
        self.assertIn('action="/streams/events/clear"', page)

    def test_clear_monitor_events_removes_only_selected_stream_events(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor.json"
            path.write_text(
                json.dumps(
                    {
                        "updatedAt": "2026-07-07T00:00:00Z",
                        "alarms": [],
                        "events": [
                            {"id": "1", "streamId": "stream-1", "type": "alarm_raised"},
                            {"id": "2", "streamId": "stream-2", "type": "alarm_raised"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state = GuiState(monitor_state_path=str(path))

            cleared = clear_monitor_events(state, "stream-1")
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertTrue(cleared)
        self.assertEqual(payload["events"], [{"id": "2", "streamId": "stream-2", "type": "alarm_raised"}])

    def test_worker_assignments_split_running_streams_across_active_workers(self):
        state = GuiState()
        state.create_stream(name="Camera A", source="external", external_url="srt://camera-a.local:9000?mode=caller")
        state.create_stream(name="Camera B", source="external", external_url="srt://camera-b.local:9000?mode=caller")

        worker_assignments_payload(state, "worker-a", "http://master:8080")
        worker_assignments_payload(state, "worker-b", "http://master:8080")
        a_payload = worker_assignments_payload(state, "worker-a", "http://master:8080")
        b_payload = worker_assignments_payload(state, "worker-b", "http://master:8080")

        self.assertEqual([item["id"] for item in a_payload["workers"]], ["worker-a", "worker-b"])
        self.assertEqual([stream["id"] for stream in a_payload["streams"]], ["stream-1"])
        self.assertEqual([stream["id"] for stream in b_payload["streams"]], ["stream-2"])
        self.assertEqual(a_payload["streams"][0]["assignedWorkerId"], "worker-a")
        self.assertEqual(b_payload["streams"][0]["assignedWorkerId"], "worker-b")

    def test_monitoring_configuration_change_advances_assignment_fence(self):
        state = GuiState()
        state.create_stream(name="Camera A", source="external", external_url="srt://camera-a.local:9000?mode=caller")
        before = worker_assignments_payload(state, "worker-a", "http://master:8080")

        state.update_alert_profile("stream-1", ["feed_reachable"], 15)
        after = worker_assignments_payload(state, "worker-a", "http://master:8080")

        self.assertGreater(after["assignmentGeneration"], before["assignmentGeneration"])
        self.assertNotEqual(after["assignmentToken"], before["assignmentToken"])

    def test_delete_racing_persistent_mutations_cannot_resurrect_stream(self):
        def exercise(label, mutate):
            with self.subTest(mutation=label), tempfile.TemporaryDirectory() as directory:
                store = SqliteFeedStore(Path(directory) / "feeds.sqlite3")
                state = GuiState(feed_store=store)
                stream = state.create_stream(
                    name="Race target",
                    source="external",
                    external_url="srt://camera.example.test:9000?mode=caller",
                )
                reached_guard = threading.Event()
                resume_mutation = threading.Event()
                original_guard = state._ensure_current_stream_locked

                def release_for_delete(candidate, operation):
                    if candidate is stream and not reached_guard.is_set():
                        reached_guard.set()
                        state.control_plane_lock.release()
                        try:
                            if not resume_mutation.wait(timeout=2):
                                raise RuntimeError("timed out waiting for concurrent delete")
                        finally:
                            state.control_plane_lock.acquire()
                    return original_guard(candidate, operation)

                result = []
                with patch.object(
                    state,
                    "_ensure_current_stream_locked",
                    side_effect=release_for_delete,
                ):
                    thread = threading.Thread(
                        target=lambda: result.append(mutate(state, stream.id)), daemon=True
                    )
                    thread.start()
                    self.assertTrue(reached_guard.wait(timeout=2))
                    self.assertTrue(state.delete_stream(stream.id))
                    resume_mutation.set()
                    thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
                self.assertEqual(result, [False])
                self.assertNotIn(stream.id, state.streams)
                self.assertEqual(store.load(), [])

        exercise(
            "mode",
            lambda state, stream_id: state.apply_mode(
                "video_only", stream_id=stream_id
            ),
        )
        exercise(
            "alert profile",
            lambda state, stream_id: state.update_alert_profile(
                stream_id, ["feed_reachable"], 5
            ),
        )
        exercise(
            "feed update",
            lambda state, stream_id: state.update_stream(
                stream_id,
                name="Stale update",
                source="external",
                protocol="srt",
                mode="normal",
                external_url="srt://camera.example.test:9000?mode=caller",
            ),
        )

    def test_concurrent_start_and_delete_cannot_leave_detached_process(self):
        state = GuiState()
        stream = state.create_stream(name="Generated")
        process = Mock(pid=4321, stdout=[])
        process.poll.return_value = None
        launch_entered = threading.Event()
        release_launch = threading.Event()

        def launch(*args, **kwargs):
            launch_entered.set()
            self.assertTrue(release_launch.wait(timeout=2))
            return process

        with patch("videosim.gui.subprocess.Popen", side_effect=launch), patch("videosim.gui.os.killpg") as killpg:
            with ThreadPoolExecutor(max_workers=2) as executor:
                start = executor.submit(state.start, stream.id)
                self.assertTrue(launch_entered.wait(timeout=2))
                delete = executor.submit(state.delete_stream, stream.id)
                time.sleep(0.02)
                self.assertFalse(delete.done())
                release_launch.set()
                self.assertTrue(start.result(timeout=2))
                self.assertTrue(delete.result(timeout=2))

        self.assertNotIn(stream.id, state.streams)
        self.assertIsNone(stream.process)
        killpg.assert_called_once_with(4321, signal.SIGINT)

    def test_unknown_stream_target_never_falls_back_to_selected_feed(self):
        state = GuiState(feed_port=9912)
        stream = self.create_feed(state)
        process = Mock(pid=1234)
        process.poll.return_value = None
        stream.process = process
        stream.started_at = time.monotonic()
        state.select_stream(stream.id)

        self.assertFalse(state.apply_mode("video_only", stream_id="missing-stream"))
        self.assertFalse(state.start("missing-stream"))
        self.assertFalse(state.stop("missing-stream"))
        self.assertFalse(state.validate("missing-stream"))
        self.assertEqual(stream.mode, "normal")
        self.assertIs(stream.process, process)

    def test_stop_handles_process_exit_race_and_cleanup_failures(self):
        state = GuiState(feed_port=9912)
        stream = self.create_feed(state)
        process = Mock(pid=1234)
        process.poll.return_value = None
        process.wait.return_value = 0
        stream.process = process
        stream.started_at = time.monotonic()
        with patch("videosim.gui.os.killpg", side_effect=ProcessLookupError):
            self.assertTrue(state.stop(stream.id))
        self.assertIsNone(stream.process)

        process = Mock(pid=1235)
        process.poll.return_value = None
        stream.process = process
        stream.started_at = time.monotonic()
        with patch("videosim.gui.os.killpg", side_effect=PermissionError("denied")):
            self.assertFalse(state.stop(stream.id))
        self.assertIs(stream.process, process)
        self.assertIn("cleanup failed", stream.last_error)

        process = Mock(pid=1236)
        process.poll.return_value = None
        process.wait.side_effect = [
            subprocess.TimeoutExpired("feed", 10),
            subprocess.TimeoutExpired("feed", 5),
        ]
        stream.process = process
        stream.started_at = time.monotonic()
        with patch("videosim.gui.os.killpg"):
            self.assertFalse(state.stop(stream.id))
        self.assertIs(stream.process, process)
        self.assertIn("did not exit after SIGKILL", stream.last_error)

    def test_concurrent_stream_creates_reserve_unique_ids_and_ports(self):
        state = GuiState(feed_port=12000)

        with ThreadPoolExecutor(max_workers=8) as executor:
            streams = list(
                executor.map(
                    lambda index: state.create_stream(name=f"Feed {index}", select=False),
                    range(40),
                )
            )

        self.assertEqual(len({stream.id for stream in streams}), 40)
        self.assertEqual(len({stream.feed_port for stream in streams}), 40)
        self.assertEqual(len(state.streams), 40)

    def test_assignment_reads_remain_consistent_during_concurrent_creates(self):
        state = GuiState(feed_port=13000)
        worker_assignments_payload(state, "worker-a", "http://master:8080")

        def create(index):
            return state.create_stream(
                name=f"Feed {index}",
                source="external",
                external_url=f"srt://camera-{index}.local:9000?mode=caller",
                select=False,
            )

        def read_assignments(_):
            return worker_assignments_payload(state, "worker-a", "http://master:8080")

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(create, index) for index in range(30)]
            futures.extend(executor.submit(read_assignments, index) for index in range(30))
            for future in futures:
                future.result()

        final = worker_assignments_payload(state, "worker-a", "http://master:8080")
        self.assertEqual({stream["id"] for stream in final["streams"]}, set(state.streams))

    def test_concurrent_worker_assignment_reads_share_one_generation_and_unique_owners(self):
        state = GuiState()
        for index in range(40):
            state.create_stream(
                name=f"Camera {index}",
                source="external",
                external_url=f"srt://camera-{index}.local:9000?mode=caller",
                select=False,
            )
        worker_ids = [f"worker-{index}" for index in range(4)]
        for worker_id in worker_ids:
            worker_assignments_payload(state, worker_id, "http://master:8080")

        with ThreadPoolExecutor(max_workers=4) as executor:
            payloads = list(
                executor.map(
                    lambda worker_id: worker_assignments_payload(state, worker_id, "http://master:8080"),
                    worker_ids,
                )
            )

        self.assertEqual(len({payload["assignmentGeneration"] for payload in payloads}), 1)
        owners = [stream["id"] for payload in payloads for stream in payload["streams"]]
        self.assertEqual(len(owners), 40)
        self.assertEqual(len(set(owners)), 40)

    def test_worker_assignment_rewrites_generated_dash_monitor_endpoint_to_master_url(self):
        state = GuiState(http_port=8080)
        stream = state.create_stream(name="Dash", protocol="dash")
        stream.process = Mock(stdout=[], pid=123)
        stream.process.poll.return_value = None

        payload = worker_assignments_payload(state, "worker-a", "http://master:8080")

        self.assertEqual(payload["streams"][0]["endpoint"], "http://master:8080/dash/stream-1/manifest.mpd")
        self.assertEqual(payload["streams"][0]["monitorEndpoint"], "http://master:8080/dash/stream-1/manifest.mpd")

    def test_apply_worker_report_replaces_only_assigned_stream_monitor_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor.json"
            path.write_text(
                json.dumps(
                    {
                        "updatedAt": "old",
                        "alarms": [
                            {"id": "stream-1:old", "streamId": "stream-1"},
                            {"id": "stream-2:old", "streamId": "stream-2"},
                        ],
                        "events": [
                            {"id": "event-1", "streamId": "stream-1"},
                            {"id": "event-2", "streamId": "stream-2"},
                        ],
                        "pending": [
                            {"id": "pending-1", "streamId": "stream-1"},
                            {"id": "pending-2", "streamId": "stream-2"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state = GuiState(monitor_state_path=str(path))
            state.create_stream(name="Camera A", source="external", external_url="srt://camera-a.local:9000?mode=caller")
            state.create_stream(name="Camera B", source="external", external_url="srt://camera-b.local:9000?mode=caller")
            assignment = worker_assignments_payload(state, "worker-a", "http://master:8080")

            response = apply_worker_report(
                state,
                "worker-a",
                ["stream-1"],
                {
                    "updatedAt": "new",
                    "alarms": [{"id": "stream-1:new", "streamId": "stream-1"}],
                    "events": [{"id": "event-new", "streamId": "stream-1"}],
                    "pending": [{"id": "pending-new", "streamId": "stream-1"}],
                },
                assignment,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(response["streamIds"], ["stream-1"])
        self.assertEqual([alarm["id"] for alarm in payload["alarms"]], ["stream-2:old", "stream-1:new"])
        self.assertEqual(payload["alarms"][1]["workerId"], "worker-a")
        self.assertEqual([event["id"] for event in payload["events"]], ["event-2", "event-new"])
        self.assertEqual(payload["events"][1]["workerId"], "worker-a")
        self.assertEqual([item["id"] for item in payload["pending"]], ["pending-2", "pending-new"])
        self.assertEqual(payload["workers"][0]["id"], "worker-a")
        self.assertFalse(response["legacyContract"])
        self.assertEqual(response["rejectedStreamIds"], [])

    def test_worker_report_rejects_stale_generation_without_mutating_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor.json"
            original = {"updatedAt": "old", "alarms": [], "events": [], "pending": []}
            path.write_text(json.dumps(original), encoding="utf-8")
            state = GuiState(monitor_state_path=str(path), control_plane_instance_id="master-a")
            state.create_stream(name="Camera A", source="external", external_url="srt://camera-a.local:9000?mode=caller")
            stale = worker_assignments_payload(state, "worker-a", "http://master:8080")
            worker_assignments_payload(state, "worker-b", "http://master:8080")

            with self.assertRaises(WorkerReportConflict):
                apply_worker_report(state, "worker-a", ["stream-1"], empty_worker_state(), stale)

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)

    def test_worker_report_rejects_assignment_token_mismatch(self):
        state = GuiState(control_plane_instance_id="master-a")
        state.create_stream(name="Camera A", source="external", external_url="srt://camera-a.local:9000?mode=caller")
        assignment = worker_assignments_payload(state, "worker-a", "http://master:8080")
        assignment["assignmentToken"] = "forged"

        with self.assertRaises(WorkerReportConflict):
            apply_worker_report(state, "worker-a", ["stream-1"], empty_worker_state(), assignment)

    def test_worker_report_rejects_non_integer_assignment_generation(self):
        for invalid in (True, "3", 3.0):
            with self.subTest(invalid=invalid):
                state = GuiState(control_plane_instance_id="master-a")
                state.create_stream(name="Camera A", source="external", external_url="srt://camera-a.local:9000?mode=caller")
                assignment = worker_assignments_payload(state, "worker-a", "http://master:8080")
                assignment["assignmentGeneration"] = invalid

                with self.assertRaises(WorkerReportValidationError):
                    apply_worker_report(state, "worker-a", ["stream-1"], empty_worker_state(), assignment)

    def test_worker_report_rejects_control_plane_instance_mismatch(self):
        state = GuiState(control_plane_instance_id="master-a")
        state.create_stream(name="Camera A", source="external", external_url="srt://camera-a.local:9000?mode=caller")
        assignment = worker_assignments_payload(state, "worker-a", "http://master:8080")
        assignment["controlPlaneInstanceId"] = "master-before-restart"

        with self.assertRaises(WorkerReportConflict):
            apply_worker_report(state, "worker-a", ["stream-1"], empty_worker_state(), assignment)

    def test_expired_worker_must_refetch_before_reporting_after_rejoin(self):
        state = GuiState(control_plane_instance_id="master-a")
        state.create_stream(name="Camera A", source="external", external_url="srt://camera-a.local:9000?mode=caller")
        old_assignment = worker_assignments_payload(state, "worker-a", "http://master:8080")
        with state.control_plane_lock:
            state.worker_seen["worker-a"] = 0
        self.assertEqual(state.worker_seen.get("worker-a"), 0)
        with patch("videosim.gui.time.time", return_value=1000):
            current_assignment = worker_assignments_payload(state, "worker-a", "http://master:8080")
            self.assertGreater(current_assignment["assignmentGeneration"], old_assignment["assignmentGeneration"])
            with self.assertRaises(WorkerReportConflict):
                apply_worker_report(state, "worker-a", ["stream-1"], empty_worker_state(), old_assignment)

    def test_assignment_generation_prevents_aba_topology_report(self):
        state = GuiState(control_plane_instance_id="master-a")
        state.create_stream(name="Camera A", source="external", external_url="srt://camera-a.local:9000?mode=caller")
        old_assignment = worker_assignments_payload(state, "worker-a", "http://master:8080")
        worker_assignments_payload(state, "worker-b", "http://master:8080")
        with state.control_plane_lock:
            state.worker_seen.pop("worker-b")
            state.assignment_generation += 1
            state.assignment_dirty = True
        current_assignment = worker_assignments_payload(state, "worker-a", "http://master:8080")

        self.assertEqual(
            [item["id"] for item in old_assignment["streams"]],
            [item["id"] for item in current_assignment["streams"]],
        )
        self.assertGreater(current_assignment["assignmentGeneration"], old_assignment["assignmentGeneration"])
        with self.assertRaises(WorkerReportConflict):
            apply_worker_report(state, "worker-a", ["stream-1"], empty_worker_state(), old_assignment)

    def test_worker_report_scopes_claims_and_every_state_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor.json"
            path.write_text(
                json.dumps(
                    {
                        "updatedAt": "old",
                        "alarms": [{"id": "stream-2:old", "streamId": "stream-2"}],
                        "events": [{"id": "event-2-old", "streamId": "stream-2"}],
                        "pending": [{"id": "pending-2-old", "streamId": "stream-2"}],
                    }
                ),
                encoding="utf-8",
            )
            state = GuiState(monitor_state_path=str(path), control_plane_instance_id="master-a")
            state.create_stream(name="Camera A", source="external", external_url="srt://camera-a.local:9000?mode=caller")
            state.create_stream(name="Camera B", source="external", external_url="srt://camera-b.local:9000?mode=caller")
            worker_assignments_payload(state, "worker-a", "http://master:8080")
            worker_assignments_payload(state, "worker-b", "http://master:8080")
            assignment = worker_assignments_payload(state, "worker-a", "http://master:8080")
            worker_state = {
                "updatedAt": "new",
                "alarms": [
                    {"id": "stream-1:new", "streamId": "stream-1"},
                    {"id": "stream-2:forged", "streamId": "stream-2"},
                ],
                "events": [
                    {"id": "event-1-new", "streamId": "stream-1"},
                    {"id": "event-2-forged", "streamId": "stream-2"},
                ],
                "pending": [
                    {"id": "pending-1-new", "streamId": "stream-1"},
                    {"id": "pending-2-forged", "streamId": "stream-2"},
                ],
                "probeMetrics": {
                    "observedAt": "new",
                    "batchDurationMs": 50,
                    "streamCount": 2,
                    "checkCount": 2,
                    "outcomes": {"success": 2},
                    "streams": [
                        {"streamId": "stream-1", "check": "validation", "outcome": "success"},
                        {"streamId": "stream-2", "check": "validation", "outcome": "success"},
                    ],
                },
            }

            response = apply_worker_report(
                state,
                "worker-a",
                ["stream-1", "stream-2"],
                worker_state,
                assignment,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(response["streamIds"], ["stream-1"])
        self.assertEqual(response["rejectedStreamIds"], ["stream-2"])
        self.assertEqual(response["droppedItems"]["alarms"], ["stream-2:forged"])
        self.assertEqual(response["droppedItems"]["events"], ["event-2-forged"])
        self.assertEqual(response["droppedItems"]["pending"], ["pending-2-forged"])
        self.assertEqual(response["droppedItems"]["probeMetrics"], ["stream-2:validation"])
        worker_metrics = payload["workerProbeMetrics"]["worker-a"]
        self.assertEqual(worker_metrics["streamCount"], 1)
        self.assertEqual(worker_metrics["checkCount"], 1)
        self.assertEqual(worker_metrics["outcomes"], {"success": 1})
        self.assertEqual([item["streamId"] for item in worker_metrics["streams"]], ["stream-1"])
        self.assertEqual({item["id"] for item in payload["alarms"]}, {"stream-1:new", "stream-2:old"})
        self.assertEqual({item["id"] for item in payload["events"]}, {"event-1-new", "event-2-old"})
        self.assertEqual({item["id"] for item in payload["pending"]}, {"pending-1-new", "pending-2-old"})

    def test_worker_report_retires_probe_metrics_for_inactive_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor.json"
            path.write_text(
                json.dumps(
                    {
                        "updatedAt": "old",
                        "alarms": [],
                        "events": [],
                        "pending": [],
                        "workerProbeMetrics": {"expired-worker": {"checkCount": 99}},
                    }
                ),
                encoding="utf-8",
            )
            state = GuiState(monitor_state_path=str(path))
            state.create_stream(name="Camera A", source="external", external_url="srt://camera-a.local:9000?mode=caller")
            assignment = worker_assignments_payload(state, "worker-a", "http://master:8080")

            apply_worker_report(state, "worker-a", ["stream-1"], empty_worker_state(), assignment)
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["workerProbeMetrics"], {})

    def test_worker_report_does_not_acknowledge_monitor_persistence_failure(self):
        state = GuiState()
        state.create_stream(name="Camera A", source="external", external_url="srt://camera-a.local:9000?mode=caller")
        assignment = worker_assignments_payload(state, "worker-a", "http://master:8080")

        with patch("videosim.gui.write_monitor_payload", return_value=False), self.assertRaises(WorkerReportPersistenceError):
            apply_worker_report(state, "worker-a", ["stream-1"], empty_worker_state(), assignment)

    def test_worker_report_rejects_malformed_monitor_items(self):
        state = GuiState()
        state.create_stream(name="Camera A", source="external", external_url="srt://camera-a.local:9000?mode=caller")
        assignment = worker_assignments_payload(state, "worker-a", "http://master:8080")

        with self.assertRaises(WorkerReportValidationError):
            apply_worker_report(
                state,
                "worker-a",
                ["stream-1"],
                {"updatedAt": "new", "alarms": [{"id": "missing-stream"}], "events": [], "pending": []},
                assignment,
            )

    def test_legacy_worker_report_is_visible_and_still_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            state = GuiState(
                monitor_state_path=str(Path(directory) / "monitor.json"),
                allow_legacy_worker_reports=True,
            )
            state.create_stream(name="Camera A", source="external", external_url="srt://camera-a.local:9000?mode=caller")
            worker_assignments_payload(state, "worker-a", "http://master:8080")

            response = apply_worker_report(
                state,
                "worker-a",
                ["stream-1", "stream-forged"],
                empty_worker_state(),
            )

        self.assertTrue(response["legacyContract"])
        self.assertEqual(response["streamIds"], ["stream-1"])
        self.assertEqual(response["rejectedStreamIds"], ["stream-forged"])

    def test_unversioned_worker_report_can_be_disabled(self):
        state = GuiState(allow_legacy_worker_reports=False)
        state.create_stream(name="Camera A", source="external", external_url="srt://camera-a.local:9000?mode=caller")
        worker_assignments_payload(state, "worker-a", "http://master:8080")

        with self.assertRaises(WorkerReportConflict):
            apply_worker_report(state, "worker-a", ["stream-1"], empty_worker_state())

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
        self.assertIn('name="source"', source)
        self.assertIn('name="external_url"', source)
        self.assertIn('source !== "external"', source)
        self.assertIn('configSource !== "external"', source)
        self.assertIn("state.framerates", source)
        self.assertIn("AlertProfileCard", source)
        self.assertIn('name="alert_monitor"', source)
        self.assertIn('name="alert_delay_seconds"', source)
        self.assertIn('value="enable_all"', source)
        self.assertIn('value="disable_all"', source)
        self.assertIn("alertState", source)

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
        self.assertIn('className="event-audit"', source)
        self.assertIn('action="/streams/events/clear"', source)
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

    def test_external_stream_payload_uses_registered_endpoint_without_process(self):
        state = GuiState(feed_port=9912)
        stream = state.create_stream(
            name="Camera",
            protocol="srt",
            source="external",
            external_url="srt://camera.local:9999?mode=caller",
        )

        payload = state_payload(state)

        self.assertEqual(stream.status, "running")
        self.assertEqual(payload["endpoint"], "srt://camera.local:9999?mode=caller")
        self.assertEqual(payload["streams"][0]["source"], "external")
        self.assertEqual(payload["streams"][0]["externalUrl"], "srt://camera.local:9999?mode=caller")
        self.assertFalse(payload["streams"][0]["alertProfile"]["allEnabled"])
        self.assertEqual(payload["streams"][0]["alertProfile"]["enabledMonitorIds"], [])
        self.assertFalse(payload["previewAvailable"])
        self.assertFalse(payload["streams"][0]["previewAvailable"])
        self.assertEqual(payload["streams"][0]["metrics"]["bitrateBps"], 0)
        self.assertEqual(payload["streams"][0]["metrics"]["videoFrames"], 0)
        with patch("videosim.gui.subprocess.Popen") as popen:
            self.assertTrue(state.start(stream.id))
        popen.assert_not_called()

    def test_external_url_validation_rejects_protocol_mismatch(self):
        state = GuiState()

        with self.assertRaisesRegex(ValueError, "External DASH URL"):
            state.create_stream(name="Bad", protocol="dash", source="external", external_url="srt://camera.local:9999?mode=caller")

    def test_gui_state_persists_feed_registrations_to_sqlite_store(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feeds.sqlite3"
            state = GuiState(feed_store=SqliteFeedStore(path))
            created = state.create_stream(
                name="External camera",
                protocol="srt",
                source="external",
                external_url="srt://camera.local:9999?mode=caller",
            )
            state.update_alert_profile(created.id, ["essence_video_present"], 4)

            reloaded = GuiState(feed_store=SqliteFeedStore(path))

        self.assertEqual(reloaded.selected_stream_id, "stream-1")
        self.assertEqual(reloaded.endpoint, "srt://camera.local:9999?mode=caller")
        self.assertEqual(reloaded.active_stream.source, "external")
        self.assertEqual(reloaded.active_stream.alert_enabled_ids, ["essence_video_present"])
        self.assertEqual(reloaded.active_stream.alert_delay_seconds, 4)

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

    def test_stream_alert_profile_update_changes_selected_stream(self):
        state = GuiState(feed_port=9912)
        self.create_feed(state)

        updated = state.update_alert_profile("stream-1", ["feed_reachable", "essence_video_present"], 15)

        self.assertTrue(updated)
        profile = state_payload(state)["streams"][0]["alertProfile"]
        self.assertFalse(profile["allEnabled"])
        self.assertEqual(profile["enabledMonitorIds"], ["feed_reachable", "essence_video_present"])
        self.assertEqual(profile["delaySeconds"], 15)

        state.update_alert_profile("stream-1", [], 0)
        self.assertEqual(state_payload(state)["streams"][0]["alertProfile"]["enabledMonitorIds"], [])

        state.update_alert_profile("stream-1", None, 0)
        self.assertTrue(state_payload(state)["streams"][0]["alertProfile"]["allEnabled"])

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

    def test_start_clears_stale_error_before_logging(self):
        state = GuiState(feed_port=9912)
        self.create_feed(state)
        state.last_error = "old error"
        state.active_stream.last_error = "old error"

        with patch("videosim.gui.subprocess.Popen") as popen:
            popen.return_value.stdout = []
            popen.return_value.pid = 1234
            popen.return_value.poll.return_value = None
            state.start()

        self.assertEqual(state.last_error, "")
        self.assertEqual(state.active_stream.last_error, "")
        self.assertEqual(state_payload(state)["streams"][0]["lastError"], "none")

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
