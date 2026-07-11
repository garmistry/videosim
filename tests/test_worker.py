import json
import tempfile
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from unittest.mock import Mock, patch

from videosim.gui import GuiState, apply_worker_report, register_worker, worker_assignments_payload
from videosim.monitor import monitor_state_for_stream_ids
from videosim.worker import build_ssl_context, call_with_retry, post_report, run_worker


class RetryableServiceError(HTTPError):
    def __init__(self):
        Exception.__init__(self, "service unavailable")
        self.code = 503


class AssignmentConflict(HTTPError):
    def __init__(self):
        Exception.__init__(self, "assignment conflict")
        self.code = 409


def assignment(stream_ids=("stream-1",), generation=7):
    return {
        "apiVersion": "videosim.worker/v1",
        "controlPlaneInstanceId": "master-a",
        "assignmentGeneration": generation,
        "assignmentToken": f"token-{generation}",
        "streams": [{"id": stream_id, "status": "running"} for stream_id in stream_ids],
    }


class WorkerTest(unittest.TestCase):
    def test_run_worker_once_monitors_assigned_streams_and_posts_report(self):
        assignments = assignment()
        monitor_state = {"updatedAt": "now", "alarms": [], "events": [], "pending": []}

        with patch("videosim.worker.fetch_assignments", return_value=assignments) as fetch, patch(
            "videosim.worker.run_monitor_once", return_value=monitor_state
        ) as monitor, patch("videosim.worker.post_report", return_value={"ok": True}) as post, patch(
            "videosim.worker.time.time", return_value=123.0
        ):
            code = run_worker("http://master:8080", "worker-a", 5, 7, 20, "app", once=True)

        self.assertEqual(code, 0)
        fetch.assert_called_once_with("http://master:8080", "worker-a", None)
        monitor.assert_called_once()
        self.assertEqual(monitor.call_args.args[0], {"streams": assignments["streams"]})
        self.assertEqual(monitor.call_args.args[2], 123.0)
        self.assertEqual(monitor.call_args.args[3], 7)
        self.assertEqual(monitor.call_args.args[4], 20)
        self.assertEqual(monitor.call_args.args[5], "app")
        post.assert_called_once_with("http://master:8080", "worker-a", ["stream-1"], monitor_state, assignments, None)

    def test_heartbeat_keeps_worker_active_during_probe_batch_longer_than_ttl(self):
        with tempfile.TemporaryDirectory() as directory:
            state = GuiState(
                monitor_state_path=str(Path(directory) / "monitor.json"),
                control_plane_instance_id="master-a",
                allow_legacy_worker_reports=False,
            )
            state.create_stream(
                name="Camera A",
                source="external",
                external_url="srt://camera-a.local:9000?mode=caller",
            )

            def fetch(control_plane_url, worker_id, ssl_context):
                return worker_assignments_payload(state, worker_id, "http://master:8080")

            def heartbeat(control_plane_url, worker_id, ssl_context):
                return register_worker(state, worker_id)

            def slow_monitor(gui_state, monitor_state, *args):
                time.sleep(0.05)
                return {"updatedAt": "now", "alarms": [], "events": [], "pending": []}

            def report(control_plane_url, worker_id, stream_ids, monitor_state, assignments, ssl_context):
                return apply_worker_report(state, worker_id, stream_ids, monitor_state, assignments)

            with patch("videosim.gui.WORKER_TTL_SECONDS", 0.02), patch(
                "videosim.worker.fetch_assignments", side_effect=fetch
            ), patch("videosim.worker.post_heartbeat", side_effect=heartbeat) as heartbeat_call, patch(
                "videosim.worker.run_monitor_once", side_effect=slow_monitor
            ), patch("videosim.worker.post_report", side_effect=report):
                code = run_worker(
                    "http://master:8080",
                    "worker-a",
                    5,
                    7,
                    20,
                    "app",
                    once=True,
                    heartbeat_seconds=0.005,
                )

        self.assertEqual(code, 0)
        self.assertGreaterEqual(heartbeat_call.call_count, 2)

    def test_worker_prunes_retained_state_for_empty_assignment(self):
        retained = {
            "updatedAt": "old",
            "alarms": [{"id": "alarm-1", "streamId": "stream-1"}],
            "events": [{"id": "event-1", "streamId": "stream-1"}],
            "pending": [{"id": "pending-1", "streamId": "stream-1"}],
            "probeMetrics": {
                "streamCount": 1,
                "checkCount": 1,
                "outcomes": {"success": 1},
                "streams": [{"streamId": "stream-1", "check": "validation", "outcome": "success"}],
            },
            "monitors": [{"id": "catalog"}],
        }

        scoped = monitor_state_for_stream_ids(retained, set())

        self.assertEqual(scoped["alarms"], [])
        self.assertEqual(scoped["events"], [])
        self.assertEqual(scoped["pending"], [])
        self.assertEqual(scoped["probeMetrics"]["streams"], [])
        self.assertEqual(scoped["probeMetrics"]["streamCount"], 0)
        self.assertEqual(scoped["probeMetrics"]["checkCount"], 0)
        self.assertEqual(scoped["probeMetrics"]["outcomes"], {})
        self.assertEqual(scoped["monitors"], [{"id": "catalog"}])

    def test_worker_refetches_assignment_after_http_409(self):
        old_assignment = assignment(generation=7)
        new_assignment = assignment(generation=8)
        conflict = AssignmentConflict()
        monitor_state = {"updatedAt": "now", "alarms": [], "events": [], "pending": []}

        with patch("videosim.worker.fetch_assignments", side_effect=[old_assignment, new_assignment]) as fetch, patch(
            "videosim.worker.run_monitor_once", return_value=monitor_state
        ) as monitor, patch("videosim.worker.post_report", side_effect=[conflict, {"ok": True}]) as post:
            code = run_worker("http://master:8080", "worker-a", 5, 7, 20, "app", once=True)

        self.assertEqual(code, 0)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(monitor.call_count, 2)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args.args[4]["assignmentGeneration"], 8)

    def test_worker_bounds_assignment_conflict_refetches(self):
        conflict = AssignmentConflict()
        monitor_state = {"updatedAt": "now", "alarms": [], "events": [], "pending": []}

        with patch("videosim.worker.fetch_assignments", return_value=assignment()), patch(
            "videosim.worker.run_monitor_once", return_value=monitor_state
        ), patch("videosim.worker.post_report", side_effect=conflict) as post:
            with self.assertRaises(HTTPError):
                run_worker("http://master:8080", "worker-a", 5, 7, 20, "app", once=True)

        self.assertEqual(post.call_count, 3)

    def test_transport_retry_uses_bounded_exponential_full_jitter(self):
        outcomes = iter([URLError("offline"), RetryableServiceError(), {"ok": True}])
        sleeps = []

        def operation():
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        result = call_with_retry(
            operation,
            attempts=3,
            base_seconds=0.25,
            sleep=sleeps.append,
            random_value=lambda: 0,
        )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(sleeps, [0.125, 0.25])

    def test_assignment_conflict_is_not_transport_retried(self):
        calls = []

        def operation():
            calls.append(True)
            raise AssignmentConflict()

        with self.assertRaises(HTTPError):
            call_with_retry(operation, attempts=5, sleep=lambda seconds: None)

        self.assertEqual(len(calls), 1)

    def test_build_ssl_context_loads_worker_certificate_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            ca = Path(directory) / "ca.crt"
            cert = Path(directory) / "worker.crt"
            key = Path(directory) / "worker.key"
            for path in (ca, cert, key):
                path.write_text("test", encoding="utf-8")
            context = Mock()
            with patch("videosim.worker.ssl.create_default_context", return_value=context) as create:
                result = build_ssl_context(str(ca), str(cert), str(key))

        self.assertIs(result, context)
        create.assert_called_once_with(cafile=str(ca))
        context.load_cert_chain.assert_called_once_with(certfile=str(cert), keyfile=str(key))

    def test_post_report_carries_assignment_contract(self):
        response = Mock()
        response.read.return_value = b'{"ok": true}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        assignments = assignment()

        with patch("videosim.worker.urlopen", return_value=response) as open_url:
            result = post_report(
                "http://master:8080",
                "worker-a",
                ["stream-1"],
                {"updatedAt": "now", "alarms": [], "events": [], "pending": []},
                assignments,
            )

        request = open_url.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(result, {"ok": True})
        self.assertEqual(body["apiVersion"], "videosim.worker/v1")
        self.assertEqual(body["controlPlaneInstanceId"], "master-a")
        self.assertEqual(body["assignmentGeneration"], 7)
        self.assertEqual(body["assignmentToken"], "token-7")


if __name__ == "__main__":
    unittest.main()
