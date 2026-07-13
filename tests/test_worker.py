import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet

from videosim.gui import GuiState, apply_worker_report, register_worker, worker_assignments_payload
from videosim.monitor import monitor_state_for_stream_ids
from videosim.report_spool import EncryptedReportSpool
from videosim.worker import (
    advance_lease_sequences,
    build_report_payload,
    build_ssl_context,
    call_with_retry,
    flush_report_spool,
    post_lease_acknowledgement,
    post_drain,
    post_heartbeat,
    post_report,
    run_worker,
    worker_pressure_from_state,
    worker_resource_pressure,
    worker_resource_snapshot,
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
    def spool(self, directory):
        key = Path(directory, "spool.key")
        key.write_bytes(Fernet.generate_key())
        key.chmod(0o600)
        return EncryptedReportSpool(Path(directory, "reports"), key, 100_000), key

    def test_report_spool_options_are_required_together(self):
        with self.assertRaisesRegex(ValueError, "required together"):
            run_worker(
                "http://master:8080",
                "worker-a",
                5,
                7,
                20,
                "app",
                once=True,
                report_spool_dir="/tmp/reports",
            )

    def test_worker_drain_payload_includes_incarnation_fence(self):
        response = Mock()
        response.read.return_value = b'{"ok": true}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch("videosim.worker.urlopen", return_value=response) as open_url:
            post_drain(
                "http://master:8080",
                "worker-a",
                worker_incarnation_id="00000000-0000-0000-0000-000000000001",
            )

        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "http://master:8080/api/workers/drain")
        self.assertEqual(
            json.loads(request.data),
            {
                "apiVersion": "videosim.worker/v2",
                "workerId": "worker-a",
                "workerIncarnationId": "00000000-0000-0000-0000-000000000001",
            },
        )

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
                max_srt_streams=60,
                max_dash_streams=40,
                max_concurrent_checks=4,
                max_concurrent_deep_checks=2,
                stream_budget_seconds=30,
                deep_check_interval_seconds=60,
                batch_budget_seconds=20,
                pressure={
                    "assignedStreams": 80,
                    "cycleActive": True,
                    "lastValidationDeferred": 5,
                },
            )
        payload = json.loads(open_url.call_args.args[0].data)
        self.assertEqual(
            payload["capacity"],
            {
                "maxStreams": 100,
                "maxSrtStreams": 60,
                "maxDashStreams": 40,
                "maxConcurrentChecks": 4,
                "maxConcurrentDeepChecks": 2,
                "streamBudgetSeconds": 30,
                "deepCheckIntervalSeconds": 60,
                "batchBudgetSeconds": 20,
                "pressure": {
                    "assignedStreams": 80,
                    "cycleActive": True,
                    "lastValidationDeferred": 5,
                },
            },
        )

    def test_worker_pressure_counts_only_batch_budget_deferrals(self):
        pressure = worker_pressure_from_state(
            {
                "probeMetrics": {
                    "batchDurationMs": 125.5,
                    "streams": [
                        {
                            "check": "validation",
                            "outcome": "skipped",
                            "detail": "validation phase exhausted batch budget",
                        },
                        {
                            "check": "deep_checks",
                            "outcome": "skipped",
                            "detail": "validation phase exhausted batch budget",
                        },
                        {
                            "check": "deep_checks",
                            "outcome": "skipped",
                            "detail": "deferred until later",
                        },
                    ],
                }
            },
            12,
        )

        self.assertEqual(
            pressure,
            {
                "assignedStreams": 12,
                "cycleActive": False,
                "lastValidationDeferred": 1,
                "lastDeepDeferred": 1,
                "lastBatchDurationMs": 125.5,
            },
        )

    def test_worker_resource_pressure_reports_cpu_rss_and_open_fds(self):
        usage = [
            SimpleNamespace(ru_utime=1.0, ru_stime=0.5, ru_maxrss=10),
            SimpleNamespace(ru_utime=2.0, ru_stime=1.0, ru_maxrss=20),
        ]
        with patch(
            "videosim.worker.resource.getrusage", side_effect=usage
        ), patch(
            "videosim.worker.Path.iterdir",
            return_value=iter(("fd-1", "fd-2", "fd-3")),
        ), patch(
            "videosim.worker.sys.platform", "linux"
        ):
            after = worker_resource_snapshot()

        pressure = worker_resource_pressure(
            {
                "totalCpuSeconds": 4.0,
                "processPeakRssBytes": 0,
                "childPeakRssBytes": 0,
            },
            after,
        )

        self.assertEqual(
            pressure,
            {
                "lastBatchCpuMs": 500.0,
                "processPeakRssBytes": 10 * 1024,
                "childPeakRssBytes": 20 * 1024,
                "openFileDescriptors": 3,
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

    def test_run_worker_posts_final_report_before_drain(self):
        drain_requested = threading.Event()
        order = []

        def monitor(*_args, **_kwargs):
            drain_requested.set()
            return {"updatedAt": "now", "alarms": [], "events": [], "pending": []}

        with patch(
            "videosim.worker.fetch_assignments", return_value=assignment()
        ), patch(
            "videosim.worker.run_monitor_once", side_effect=monitor
        ), patch(
            "videosim.worker.post_report",
            side_effect=lambda *_args, **_kwargs: order.append("report") or {"ok": True},
        ), patch(
            "videosim.worker.post_drain",
            side_effect=lambda *_args, **_kwargs: order.append("drain") or {"ok": True},
        ):
            code = run_worker(
                "http://master:8080",
                "worker-a",
                5,
                7,
                20,
                "app",
                once=True,
                drain_event=drain_requested,
            )

        self.assertEqual(code, 0)
        self.assertEqual(order, ["report", "drain"])

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
                    max_concurrent_deep_checks=1,
                    stream_budget_seconds=30,
                    deep_check_interval_seconds=60,
                    batch_budget_seconds=20,
                ),
                0,
            )

        self.assertEqual(
            monitor.call_args.kwargs,
            {
                "max_concurrency": 2,
                "max_deep_concurrency": 1,
                "stream_budget_seconds": 30,
                "deep_check_interval_seconds": 60,
                "batch_budget_seconds": 20,
            },
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
                {
                    "probeMetrics": {
                        "observedAt": "2026-07-11T00:00:00Z",
                        "streams": [],
                    },
                    "deepCheckSchedule": {"stream-1": 200.0},
                    "validationCursor": 2,
                },
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
        self.assertNotIn("deepCheckSchedule", report_payload["state"])
        self.assertNotIn("validationCursor", report_payload["state"])
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

            pressures = []

            def heartbeat(
                control_plane_url,
                worker_id,
                ssl_context,
                worker_incarnation_id="",
                **kwargs,
            ):
                pressures.append(kwargs["pressure"])
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
        self.assertTrue(
            any(
                item["cycleActive"] and item["assignedStreams"] == 1
                for item in pressures
            )
        )

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
            "deepCheckSchedule": {"stream-1": 200.0},
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
        self.assertEqual(scoped["deepCheckSchedule"], {})
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

    def test_worker_keeps_incarnation_across_assignment_transport_outage(self):
        monitor_state = {
            "updatedAt": "now",
            "alarms": [],
            "events": [],
            "pending": [],
        }
        drain = threading.Event()
        incarnations = []

        def fetch(*args):
            incarnations.append(args[3])
            if len(incarnations) == 1:
                raise URLError("partitioned")
            return durable_assignment()

        def report(*_args, **_kwargs):
            drain.set()
            return {"ok": True}

        with patch("videosim.worker.fetch_assignments", side_effect=fetch) as fetch_call, patch(
            "videosim.worker.post_lease_acknowledgement",
            side_effect=[URLError("partitioned"), {"ok": True}],
        ) as acknowledge, patch(
            "videosim.worker.run_monitor_once", return_value=monitor_state
        ) as monitor, patch(
            "videosim.worker.post_report", side_effect=report
        ), patch(
            "videosim.worker.post_drain", return_value={"ok": True}
        ):
            code = run_worker(
                "http://master:8080",
                "worker-a",
                0,
                7,
                20,
                "app",
                heartbeat_seconds=100,
                retry_attempts=1,
                drain_event=drain,
            )

        self.assertEqual(code, 0)
        self.assertEqual(fetch_call.call_count, 3)
        self.assertEqual(acknowledge.call_count, 2)
        self.assertEqual(monitor.call_count, 1)
        self.assertEqual(len(set(incarnations)), 1)

    def test_spooled_report_blocks_new_probe_work_until_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            spool, key = self.spool(directory)
            spool.enqueue(
                build_report_payload(
                    "worker-a",
                    ["stream-1"],
                    {"updatedAt": "now"},
                    durable_assignment(),
                    worker_incarnation_id="00000000-0000-0000-0000-000000000001",
                    sequence=1,
                    report_id="00000000-0000-0000-0000-000000000002",
                )
            )
            with patch(
                "videosim.worker.post_report_payload", side_effect=URLError("offline")
            ), patch("videosim.worker.fetch_assignments") as fetch, patch(
                "videosim.worker.run_monitor_once"
            ) as monitor:
                code = run_worker(
                    "http://master:8080",
                    "worker-a",
                    5,
                    7,
                    20,
                    "app",
                    once=True,
                    retry_attempts=1,
                    report_spool_dir=str(spool.directory),
                    report_spool_key_file=str(key),
                    report_spool_max_bytes=100_000,
                )

        self.assertEqual(code, 1)
        fetch.assert_not_called()
        monitor.assert_not_called()

    def test_startup_replays_spool_before_registering_new_incarnation(self):
        order = []
        monitor_state = {
            "updatedAt": "now",
            "alarms": [],
            "events": [],
            "pending": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            spool, key = self.spool(directory)
            spool.enqueue(
                build_report_payload(
                    "worker-a",
                    ["stream-1"],
                    {"updatedAt": "old"},
                    durable_assignment(),
                    worker_incarnation_id="00000000-0000-0000-0000-000000000001",
                    sequence=1,
                    report_id="00000000-0000-0000-0000-000000000002",
                )
            )
            with patch(
                "videosim.worker.post_report_payload",
                side_effect=lambda *_args: order.append("report") or {"ok": True},
            ), patch(
                "videosim.worker.post_heartbeat",
                side_effect=lambda *_args, **_kwargs: order.append("heartbeat") or {"ok": True},
            ), patch(
                "videosim.worker.fetch_assignments",
                return_value=durable_assignment(()),
            ), patch(
                "videosim.worker.post_lease_acknowledgement", return_value={"ok": True}
            ), patch(
                "videosim.worker.run_monitor_once", return_value=monitor_state
            ):
                code = run_worker(
                    "http://master:8080",
                    "worker-a",
                    5,
                    7,
                    20,
                    "app",
                    once=True,
                    retry_attempts=1,
                    max_streams=100,
                    report_spool_dir=str(spool.directory),
                    report_spool_key_file=str(key),
                    report_spool_max_bytes=100_000,
                )

        self.assertEqual(code, 0)
        self.assertEqual(order[:2], ["report", "heartbeat"])

    def test_worker_write_ahead_report_recovers_after_transport_outage(self):
        monitor_state = {
            "updatedAt": "now",
            "alarms": [],
            "events": [],
            "pending": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            spool, key = self.spool(directory)
            with patch(
                "videosim.worker.fetch_assignments", return_value=durable_assignment()
            ), patch(
                "videosim.worker.post_lease_acknowledgement", return_value={"ok": True}
            ), patch(
                "videosim.worker.run_monitor_once", return_value=monitor_state
            ), patch(
                "videosim.worker.post_report_payload", side_effect=URLError("offline")
            ):
                code = run_worker(
                    "http://master:8080",
                    "worker-a",
                    5,
                    7,
                    20,
                    "app",
                    once=True,
                    retry_attempts=1,
                    report_spool_dir=str(spool.directory),
                    report_spool_key_file=str(key),
                    report_spool_max_bytes=100_000,
                )

            reopened = EncryptedReportSpool(spool.directory, key, 100_000)
            queued = reopened.read(reopened.entries()[0])
            self.assertEqual(code, 1)
            self.assertEqual(queued["streamIds"], ["stream-1"])
            self.assertEqual(queued["sequence"], 1)
            with patch(
                "videosim.worker.post_report_payload", return_value={"ok": True}
            ) as post:
                disposition = flush_report_spool(
                    reopened, "http://master:8080", None, 1, 0
                )

            self.assertEqual(disposition, "delivered")
            self.assertEqual(post.call_args.args[1], queued)
            self.assertEqual(reopened.entries(), [])

    def test_spool_discards_explicitly_fenced_stale_report(self):
        with tempfile.TemporaryDirectory() as directory:
            spool, _key = self.spool(directory)
            spool.enqueue(
                build_report_payload(
                    "worker-a",
                    ["stream-1"],
                    {"updatedAt": "now"},
                    durable_assignment(),
                    worker_incarnation_id="00000000-0000-0000-0000-000000000001",
                    sequence=1,
                    report_id="00000000-0000-0000-0000-000000000002",
                )
            )
            with patch(
                "videosim.worker.post_report_payload", side_effect=AssignmentConflict()
            ):
                disposition = flush_report_spool(
                    spool, "http://master:8080", None, 1, 0
                )

            self.assertEqual(disposition, "stale")
            self.assertEqual(spool.entries(), [])

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
