import json
import tempfile
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from unittest.mock import Mock, patch

from videosim.gui import GuiState, apply_worker_report, register_worker, worker_assignments_payload
from videosim.monitor import monitor_state_for_stream_ids
from videosim.worker import (
    advance_lease_sequences,
    build_ssl_context,
    call_with_retry,
    post_lease_acknowledgement,
    post_heartbeat,
    post_report,
    run_worker,
)


class RetryableServiceError(HTTPError):
    def __init__(self):
        Exception.__init__(self, "service unavailable")
        self.code = 503


class AssignmentConflict(HTTPError):
    def __init__(self):
        Exception.__init__(self, "assignment conflict")
        self.code = 409


def durable_assignment(stream_ids=("stream-1",)):
    return {
        "apiVersion": "videosim.worker/v2",
        "workerIncarnationId": "00000000-0000-0000-0000-000000000001",
        "streams": [
            {
                "id": stream_id,
                "status": "running",
                "lease": {
                    "epoch": 3,
                    "configVersion": 2,
                    "expiresAt": "2030-01-01T00:00:00+00:00",
                    "state": "offered",
                },
            }
            for stream_id in stream_ids
        ],
    }


def assignment(stream_ids=("stream-1",), generation=7):
    return {
        "apiVersion": "videosim.worker/v1",
        "controlPlaneInstanceId": "master-a",
        "assignmentGeneration": generation,
        "assignmentToken": f"token-{generation}",
        "streams": [{"id": stream_id, "status": "running"} for stream_id in stream_ids],
    }


class WorkerTest(unittest.TestCase):
    def test_worker_capacity_registration_payload_is_bounded_to_admission_limit(self):
        response = Mock()
        response.read.return_value = b'{"ok": true}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch("videosim.worker.urlopen", return_value=response) as open_url:
            post_heartbeat(
                "http://master:8080",
                "worker-a",
                worker_incarnation_id="00000000-0000-0000-0000-000000000001",
                max_streams=100,
                max_concurrent_checks=4,
                stream_budget_seconds=30,
            )
        payload = json.loads(open_url.call_args.args[0].data)
        self.assertEqual(
            payload["capacity"],
            {
                "maxStreams": 100,
                "maxConcurrentChecks": 4,
                "streamBudgetSeconds": 30,
            },
        )

    def test_v2_sequences_increment_per_lease_and_reset_on_epoch_change(self):
        streams = durable_assignment(("stream-1", "stream-2"))["streams"]
        state, first = advance_lease_sequences(streams, {})
        state, second = advance_lease_sequences(streams, state)
        changed = json.loads(json.dumps(streams))
        changed[0]["lease"]["epoch"] += 1
        _, third = advance_lease_sequences(changed, state)

        self.assertEqual(first, {"stream-1": 1, "stream-2": 1})
        self.assertEqual(second, {"stream-1": 2, "stream-2": 2})
        self.assertEqual(third, {"stream-1": 1, "stream-2": 3})

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
        self.assertEqual(fetch.call_args.args[:3], ("http://master:8080", "worker-a", None))
        self.assertTrue(fetch.call_args.args[3])
        monitor.assert_called_once()
        self.assertEqual(monitor.call_args.args[0], {"streams": assignments["streams"]})
        self.assertEqual(monitor.call_args.args[2], 123.0)
        self.assertEqual(monitor.call_args.args[3], 7)
        self.assertEqual(monitor.call_args.args[4], 20)
        self.assertEqual(monitor.call_args.args[5], "app")
        self.assertEqual(
            post.call_args.args,
            ("http://master:8080", "worker-a", ["stream-1"], monitor_state, assignments, None),
        )
        self.assertTrue(post.call_args.kwargs["worker_incarnation_id"])
        self.assertEqual(post.call_args.kwargs["sequence"], 1)
        self.assertTrue(post.call_args.kwargs["report_id"])

    def test_run_worker_passes_bounded_probe_controls(self):
        assignments = assignment(("stream-1", "stream-2"))
        monitor_state = {"updatedAt": "now", "alarms": [], "events": [], "pending": []}
        with patch("videosim.worker.fetch_assignments", return_value=assignments), patch(
            "videosim.worker.post_heartbeat", return_value={"ok": True}
        ), patch(
            "videosim.worker.run_monitor_once", return_value=monitor_state
        ) as monitor, patch("videosim.worker.post_report", return_value={"ok": True}):
            self.assertEqual(
                run_worker(
                    "http://master:8080",
                    "worker-a",
                    5,
                    7,
                    20,
                    "app",
                    once=True,
                    max_concurrent_checks=2,
                    stream_budget_seconds=30,
                ),
                0,
            )

        self.assertEqual(
            monitor.call_args.kwargs,
            {"max_concurrency": 2, "stream_budget_seconds": 30},
        )

    def test_run_worker_v2_acknowledges_lease_and_posts_stable_report_identity(self):
        assignments = durable_assignment()
        monitor_state = {
            "updatedAt": "2026-07-11T00:00:00Z",
            "alarms": [],
            "events": [],
            "pending": [],
            "probeMetrics": {
                "observedAt": "2026-07-11T00:00:00Z",
                "streams": [
                    {
                        "streamId": "stream-1",
                        "check": "validation",
                        "outcome": "success",
                    }
                ],
            },
        }
        with patch(
            "videosim.worker.fetch_assignments", return_value=assignments
        ), patch(
            "videosim.worker.post_lease_acknowledgement",
            return_value={"ok": True},
        ) as acknowledge, patch(
            "videosim.worker.run_monitor_once", return_value=monitor_state
        ), patch(
            "videosim.worker.post_report", return_value={"ok": True}
        ) as report:
            code = run_worker(
                "http://master:8080", "worker-a", 5, 7, 20, "app", once=True
            )

        self.assertEqual(code, 0)
        self.assertEqual(acknowledge.call_count, 1)
        self.assertTrue(acknowledge.call_args.args[2])
        self.assertEqual(report.call_args.kwargs["sequence"], 1)
        self.assertTrue(report.call_args.kwargs["report_id"])
        self.assertEqual(
            report.call_args.kwargs["worker_incarnation_id"],
            acknowledge.call_args.args[2],
        )

    def test_worker_v2_http_payloads_include_lease_and_result_fences(self):
        response = Mock()
        response.read.return_value = b'{"ok": true}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        assignments = durable_assignment()
        with patch("videosim.worker.urlopen", return_value=response) as open_url:
            post_lease_acknowledgement(
                "http://master:8080",
                "worker-a",
                "00000000-0000-0000-0000-000000000001",
                assignments,
            )
            ack_request = open_url.call_args.args[0]
            ack_payload = json.loads(ack_request.data)
            post_report(
                "http://master:8080",
                "worker-a",
                ["stream-1"],
                {"probeMetrics": {"observedAt": "2026-07-11T00:00:00Z", "streams": []}},
                assignments,
                worker_incarnation_id="00000000-0000-0000-0000-000000000001",
                sequence=9,
                report_id="00000000-0000-0000-0000-000000000009",
            )
            report_request = open_url.call_args.args[0]
            report_payload = json.loads(report_request.data)

        self.assertEqual(ack_payload["leases"][0]["epoch"], 3)
        self.assertEqual(ack_payload["leases"][0]["configVersion"], 2)
        self.assertEqual(report_payload["sequence"], 9)
        self.assertEqual(report_payload["leases"][0]["epoch"], ack_payload["leases"][0]["epoch"])
        self.assertEqual(
            report_payload["leases"][0]["configVersion"],
            ack_payload["leases"][0]["configVersion"],
        )
        self.assertEqual(report_payload["leases"][0]["sequence"], 9)
        self.assertEqual(report_payload["reportId"], "00000000-0000-0000-0000-000000000009")

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

            def fetch(control_plane_url, worker_id, ssl_context, worker_incarnation_id=""):
                return worker_assignments_payload(state, worker_id, "http://master:8080")

            def heartbeat(control_plane_url, worker_id, ssl_context, worker_incarnation_id=""):
                return register_worker(state, worker_id)

            def slow_monitor(gui_state, monitor_state, *args):
                time.sleep(0.05)
                return {"updatedAt": "now", "alarms": [], "events": [], "pending": []}

            def report(
                control_plane_url,
                worker_id,
                stream_ids,
                monitor_state,
                assignments,
                ssl_context,
                **kwargs,
            ):
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

    def test_worker_refetches_v2_assignment_after_acknowledgement_conflict(self):
        assignments = durable_assignment()
        monitor_state = {
            "updatedAt": "now",
            "alarms": [],
            "events": [],
            "pending": [],
            "probeMetrics": {"observedAt": "2026-07-11T00:00:00Z", "streams": []},
        }
        with patch(
            "videosim.worker.fetch_assignments", side_effect=[assignments, assignments]
        ) as fetch, patch(
            "videosim.worker.post_lease_acknowledgement",
            side_effect=[AssignmentConflict(), {"ok": True}],
        ) as acknowledge, patch(
            "videosim.worker.run_monitor_once", return_value=monitor_state
        ) as monitor, patch(
            "videosim.worker.post_report", return_value={"ok": True}
        ) as report:
            code = run_worker(
                "http://master:8080", "worker-a", 5, 7, 20, "app", once=True
            )

        self.assertEqual(code, 0)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(acknowledge.call_count, 2)
        self.assertEqual(monitor.call_count, 1)
        self.assertEqual(report.call_count, 1)

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
