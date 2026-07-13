import json
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import unittest
import uuid
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from videosim.gui import GuiHandler, GuiState, OperatorMutationPersistenceError
from videosim.migrations import PostgresMigrator
from videosim.postgres_store import PostgresControlPlaneStore, PostgresStoreError
from videosim.security import SecurityConfig
from videosim.worker import run_worker


DATABASE_URL = os.environ.get("VIDEOSIM_TEST_POSTGRES_URL", "")


def probe_state(stream_id, validation_outcome="success", monitor_status=None):
    observed_at = datetime.now(timezone.utc).isoformat()
    monitor_status = monitor_status or (
        "unhealthy" if validation_outcome == "issue" else "healthy"
    )
    return {
        "updatedAt": observed_at,
        "alarms": [],
        "events": [],
        "pending": [],
        "monitorObservations": [
            {
                "streamId": stream_id,
                "monitorId": "feed_reachable",
                "status": monitor_status,
                "message": "Feed is unreachable" if monitor_status == "unhealthy" else "Feed is reachable",
            }
        ],
        "probeMetrics": {
            "observedAt": observed_at,
            "batchDurationMs": 12.5,
            "streamCount": 1,
            "checkCount": 2,
            "outcomes": {validation_outcome: 1, "skipped": 1},
            "streams": [
                {
                    "streamId": stream_id,
                    "protocol": "srt",
                    "source": "external",
                    "check": "validation",
                    "outcome": validation_outcome,
                    "durationMs": 10,
                },
                {
                    "streamId": stream_id,
                    "protocol": "srt",
                    "source": "external",
                    "check": "loudness",
                    "outcome": "skipped",
                    "durationMs": 0,
                    "detail": "not configured",
                },
            ],
        },
    }


@unittest.skipUnless(DATABASE_URL, "VIDEOSIM_TEST_POSTGRES_URL is not configured")
class DurableWorkerV2ApiIntegrationTest(unittest.TestCase):
    def setUp(self):
        PostgresMigrator(DATABASE_URL).apply()
        self.tenant_id = f"worker-v2-{uuid.uuid4()}"
        self.store = PostgresControlPlaneStore(
            DATABASE_URL,
            tenant_id=self.tenant_id,
            min_pool_size=1,
            max_pool_size=6,
        )
        with self.store._pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    "INSERT INTO tenants (id, name) VALUES (%s, %s)",
                    (self.tenant_id, "Worker v2 test"),
                )
        self.monitor_directory = tempfile.TemporaryDirectory()
        self.state = GuiState(
            feed_store=self.store,
            monitor_state_path=os.path.join(
                self.monitor_directory.name, "monitor.json"
            ),
        )
        stream = self.state.create_stream(
            name="Worker v2 external feed",
            source="external",
            external_url="srt://example.test:9000?mode=caller",
        )
        self.stream_id = stream.id
        handler = type("DurableWorkerHandler", (GuiHandler,), {"state": self.state})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        # Audit/outbox rows are database-enforced append-only. Each test uses a
        # unique tenant in a disposable integration database, so teardown must
        # not bypass that production invariant to erase evidence.
        self.store.close()
        self.monitor_directory.cleanup()

    def get_json(self, path, query, expected_status=200, headers=None):
        try:
            request = Request(
                f"{self.base_url}{path}?{urlencode(query)}",
                headers=headers or {},
                method="GET",
            )
            with urlopen(request, timeout=5) as response:
                status = response.status
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                status = exc.code
                body = json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()
        self.assertEqual(status, expected_status, body)
        return status, body

    def post_json(self, path, payload, expected_status=200, headers=None):
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        request = Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers=request_headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=5) as response:
                status = response.status
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                status = exc.code
                body = json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()
        self.assertEqual(status, expected_status, body)
        return body

    def post_form(self, path, fields, *, headers=None, expected_status=303):
        body = urlencode(fields)
        request_headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": str(len(body.encode("utf-8"))),
        }
        request_headers.update(headers or {})
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request("POST", path, body=body, headers=request_headers)
            response = connection.getresponse()
            payload = response.read().decode("utf-8")
            self.assertEqual(response.status, expected_status, payload)
            return json.loads(payload) if payload else {}
        finally:
            connection.close()

    def operator_headers(self, *, origin=True):
        headers = {
            "X-VideoSim-Proxy-Secret": "p" * 32,
            "X-VideoSim-User": "admin@example.test",
            "X-VideoSim-Groups": "videosim-admin,videosim-viewer",
            "X-VideoSim-Expected-Origin": "https://videosim.example.test",
        }
        if origin:
            headers["Origin"] = "https://videosim.example.test"
        return headers

    def worker_headers(self, worker_id):
        return {
            "X-VideoSim-Proxy-Secret": "p" * 32,
            "X-VideoSim-Worker-ID": worker_id,
            "X-Forwarded-Proto": "https",
        }

    def enable_trusted_security(self):
        self.state.security = SecurityConfig(
            mode="trusted-proxy",
            proxy_shared_secret="p" * 32,
            viewer_group="videosim-viewer",
            admin_group="videosim-admin",
        )

    def enable_feed_reachable_alert(self, delay_seconds=0):
        self.assertTrue(
            self.state.update_alert_profile(
                self.stream_id, ["feed_reachable"], delay_seconds
            )
        )

    def make_worker_stale(self, worker_id):
        with self.store._pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    UPDATE workers
                    SET last_heartbeat_at = clock_timestamp() - interval '2 minutes'
                    WHERE tenant_id = %s AND worker_id = %s
                    """,
                    (self.tenant_id, worker_id),
                )

    def assignment(self, worker_id, incarnation, expected_count=1):
        _, payload = self.get_json(
            "/api/workers/assignments",
            {
                "worker_id": worker_id,
                "worker_incarnation_id": str(incarnation),
            },
        )
        self.assertEqual(payload["apiVersion"], "videosim.worker/v2")
        self.assertEqual(len(payload["streams"]), expected_count)
        return payload

    def acknowledge(self, worker_id, incarnation, assignment):
        leases = [
            {
                "streamId": stream["id"],
                "epoch": stream["lease"]["epoch"],
                "configVersion": stream["lease"]["configVersion"],
            }
            for stream in assignment["streams"]
        ]
        return self.post_json(
            "/api/workers/leases/ack",
            {
                "apiVersion": "videosim.worker/v2",
                "workerId": worker_id,
                "workerIncarnationId": str(incarnation),
                "leases": leases,
            },
        )

    def report_payload(
        self,
        worker_id,
        incarnation,
        assignment,
        sequence,
        state=None,
        stream_id=None,
    ):
        stream_id = stream_id or self.stream_id
        stream_assignment = next(
            stream for stream in assignment["streams"] if stream["id"] == stream_id
        )
        return {
            "apiVersion": "videosim.worker/v2",
            "reportId": str(uuid.uuid4()),
            "workerId": worker_id,
            "workerIncarnationId": str(incarnation),
            "sequence": sequence,
            "streamIds": [stream_id],
            "leases": [
                {
                    "streamId": stream_id,
                    "epoch": stream_assignment["lease"]["epoch"],
                    "configVersion": stream_assignment["lease"]["configVersion"],
                    "sequence": sequence,
                }
            ],
            "state": state or probe_state(stream_id),
        }

    def test_v2_requires_verified_proxy_worker_identity(self):
        self.enable_trusted_security()
        worker_id = "worker-v2-mtls"
        incarnation = uuid.uuid4()
        _, denied = self.get_json(
            "/api/workers/assignments",
            {
                "worker_id": worker_id,
                "worker_incarnation_id": str(incarnation),
            },
            expected_status=403,
            headers=self.worker_headers("different-worker"),
        )
        _, assignment = self.get_json(
            "/api/workers/assignments",
            {
                "worker_id": worker_id,
                "worker_incarnation_id": str(incarnation),
            },
            headers=self.worker_headers(worker_id),
        )

        self.assertIn("does not match", denied["error"])
        self.assertEqual(assignment["apiVersion"], "videosim.worker/v2")
        with self.store._pool.connection() as connection:
            audits = connection.execute(
                """
                SELECT action, outcome, principal_kind, principal_subject
                FROM audit_events
                WHERE tenant_id = %s AND action = 'worker_auth'
                """,
                (self.tenant_id,),
            ).fetchall()
        self.assertEqual(
            [
                (
                    row["action"],
                    row["outcome"],
                    row["principal_kind"],
                    row["principal_subject"],
                )
                for row in audits
            ],
            [("worker_auth", "denied", "worker", "different-worker")],
        )

    def test_operator_feed_mutations_commit_atomic_success_audits(self):
        self.enable_trusted_security()
        headers = self.operator_headers()
        with patch.object(
            SecurityConfig, "validate_external_endpoint", return_value=None
        ):
            self.post_form(
                "/start",
                {
                    "stream_id": self.stream_id,
                    "protocol": "srt",
                    "mode": "video_only",
                },
                headers=headers,
            )
        self.post_form(
            "/streams/create",
            {
                "name": "Audited feed",
                "source": "generated",
                "protocol": "srt",
                "mode": "normal",
                "framerate": "30",
            },
            headers=headers,
        )
        created_id = "stream-2"
        self.post_form(
            "/streams/update",
            {
                "stream_id": created_id,
                "name": "Audited feed updated",
                "source": "generated",
                "protocol": "srt",
                "mode": "video_only",
                "framerate": "30",
            },
            headers=headers,
        )
        self.post_form(
            "/streams/alerts",
            {
                "stream_id": created_id,
                "alert_action": "save",
                "alert_monitor": "feed_reachable",
                "alert_delay_seconds": "3",
            },
            headers=headers,
        )
        self.post_form(
            "/streams/delete",
            {"stream_id": created_id},
            headers=headers,
        )

        with self.store._pool.connection() as connection:
            feed_row = connection.execute(
                "SELECT id FROM feeds WHERE tenant_id = %s AND id = %s",
                (self.tenant_id, created_id),
            ).fetchone()
            audits = connection.execute(
                """
                SELECT action, principal_subject, outcome
                FROM audit_events
                WHERE tenant_id = %s AND resource_id = %s
                ORDER BY occurred_at, action
                """,
                (self.tenant_id, created_id),
            ).fetchall()
            external_audit = connection.execute(
                """
                SELECT action, outcome FROM audit_events
                WHERE tenant_id = %s AND resource_id = %s
                  AND action = 'feed.expectation.update'
                """,
                (self.tenant_id, self.stream_id),
            ).fetchone()
            outbox_count = connection.execute(
                """
                SELECT count(*) AS count FROM outbox
                WHERE subject = 'videosim.audit.v1'
                  AND payload ->> 'tenantId' = %s
                  AND payload ->> 'resourceId' = %s
                """,
                (self.tenant_id, created_id),
            ).fetchone()["count"]
        self.assertIsNone(feed_row)
        self.assertNotIn(created_id, self.state.streams)
        self.assertEqual(
            (external_audit["action"], external_audit["outcome"]),
            ("feed.expectation.update", "succeeded"),
        )
        self.assertEqual(
            {row["action"] for row in audits},
            {
                "feed.create",
                "feed.update",
                "feed.alert_profile.update",
                "feed.delete",
            },
        )
        self.assertTrue(
            all(
                row["principal_subject"] == "admin@example.test"
                and row["outcome"] == "succeeded"
                for row in audits
            )
        )
        self.assertEqual(outbox_count, 4)

    def test_query_bearing_persistent_mutation_routes_without_auditing_query(self):
        self.enable_trusted_security()
        sentinel = "success-query-secret-must-not-persist"
        self.post_form(
            f"/streams/create?access_token={sentinel}",
            {
                "name": "Query route feed",
                "source": "generated",
                "protocol": "srt",
                "mode": "normal",
            },
            headers=self.operator_headers(),
        )
        created = self.state.streams["stream-2"]
        with self.store._pool.connection() as connection:
            audit_row = connection.execute(
                """
                SELECT resource_id, payload FROM audit_events
                WHERE tenant_id = %s AND resource_id = %s
                  AND action = 'feed.create'
                """,
                (self.tenant_id, created.id),
            ).fetchone()
        self.assertEqual(audit_row["payload"]["operation"], "/streams/create")
        self.assertNotIn(sentinel, json.dumps(audit_row["payload"]))

    def test_generated_expectation_commits_before_local_restart(self):
        self.enable_trusted_security()
        generated = self.state.create_stream(name="Generated audited feed")
        process = MagicMock(pid=43210)
        process.poll.return_value = None
        process.wait.return_value = 0
        generated.process = process
        generated.started_at = 1.0

        with patch.object(
            self.state, "_terminate_stream_process", return_value=True
        ) as terminate, patch.object(
            self.state, "start", return_value=True
        ) as start:
            self.post_form(
                "/start",
                {
                    "stream_id": generated.id,
                    "protocol": "srt",
                    "mode": "video_only",
                },
                headers=self.operator_headers(),
            )

        with self.store._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT config FROM feeds
                WHERE tenant_id = %s AND id = %s
                """,
                (self.tenant_id, generated.id),
            ).fetchone()
            audit_row = connection.execute(
                """
                SELECT action, outcome FROM audit_events
                WHERE tenant_id = %s AND resource_id = %s
                  AND action = 'feed.expectation.update'
                """,
                (self.tenant_id, generated.id),
            ).fetchone()
        self.assertEqual(row["config"]["mode"], "video_only")
        self.assertEqual(generated.mode, "video_only")
        self.assertEqual((audit_row["action"], audit_row["outcome"]), ("feed.expectation.update", "succeeded"))
        terminate.assert_called_once_with(generated)
        start.assert_called_once_with(generated.id)

    def test_generated_expectation_audit_failure_prevents_local_restart(self):
        self.enable_trusted_security()
        generated = self.state.create_stream(name="Generated fail closed")
        process = MagicMock(pid=43211)
        process.poll.return_value = None
        generated.process = process
        generated.started_at = 1.0
        allocator_before = self.state._next_stream_number

        with patch.object(
            self.store,
            "_insert_outbox",
            side_effect=PostgresStoreError("audit outbox unavailable"),
        ), patch.object(
            self.state, "_terminate_stream_process"
        ) as terminate:
            self.post_form(
                "/start",
                {
                    "stream_id": generated.id,
                    "protocol": "srt",
                    "mode": "video_only",
                },
                headers=self.operator_headers(),
                expected_status=503,
            )
        with self.store._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT config FROM feeds
                WHERE tenant_id = %s AND id = %s
                """,
                (self.tenant_id, generated.id),
            ).fetchone()
        self.assertEqual(row["config"]["mode"], "normal")
        self.assertEqual(generated.mode, "normal")
        self.assertIs(generated.process, process)
        self.assertEqual(self.state._next_stream_number, allocator_before)
        terminate.assert_not_called()

    def test_committed_update_and_delete_converge_when_process_cleanup_fails(self):
        self.enable_trusted_security()
        generated = self.state.create_stream(name="Cleanup failure feed")
        process = MagicMock(pid=43212)
        process.poll.return_value = None
        generated.process = process
        generated.started_at = 1.0
        with patch.object(
            self.state, "_terminate_stream_process", return_value=False
        ):
            self.post_form(
                "/streams/update",
                {
                    "stream_id": generated.id,
                    "name": "Committed despite cleanup failure",
                    "source": "generated",
                    "protocol": "srt",
                    "mode": "video_only",
                    "framerate": "30",
                },
                headers=self.operator_headers(),
            )
        with self.store._pool.connection() as connection:
            updated = connection.execute(
                "SELECT config FROM feeds WHERE tenant_id = %s AND id = %s",
                (self.tenant_id, generated.id),
            ).fetchone()
        self.assertEqual(updated["config"]["name"], "Committed despite cleanup failure")
        self.assertEqual(generated.name, "Committed despite cleanup failure")
        self.assertEqual(generated.mode, "video_only")
        self.assertIs(generated.process, process)
        self.assertIn("previous process could not be stopped", generated.last_error)

        cleanup_called = threading.Event()
        with patch.object(
            self.state, "_terminate_stream_process", return_value=False
        ), patch.object(
            self.state,
            "_retry_detached_process_cleanup",
            side_effect=lambda stream: cleanup_called.set(),
        ) as retry_cleanup:
            self.post_form(
                "/streams/delete",
                {"stream_id": generated.id},
                headers=self.operator_headers(),
            )
        with self.store._pool.connection() as connection:
            deleted = connection.execute(
                "SELECT id FROM feeds WHERE tenant_id = %s AND id = %s",
                (self.tenant_id, generated.id),
            ).fetchone()
        self.assertIsNone(deleted)
        self.assertNotIn(generated.id, self.state.streams)
        self.assertTrue(cleanup_called.wait(timeout=1))
        retry_cleanup.assert_called_once_with(generated)

    def test_stale_postgres_process_cannot_resurrect_deleted_or_recreated_feed(self):
        other_store = PostgresControlPlaneStore(
            DATABASE_URL,
            tenant_id=self.tenant_id,
            min_pool_size=1,
            max_pool_size=2,
        )
        other_state = GuiState(feed_store=other_store)

        def audit(action):
            return {
                "event_id": uuid.uuid4(),
                "principal_kind": "operator",
                "principal_subject": "admin@example.test",
                "action": action,
                "outcome": "succeeded",
                "occurred_at": datetime.now(timezone.utc),
                "payload": {"operation": "/test"},
            }

        try:
            self.assertEqual(self.state.streams[self.stream_id].config_version, 1)
            self.assertEqual(other_state.streams[self.stream_id].config_version, 1)
            self.assertTrue(self.state.delete_stream(self.stream_id, audit=audit("feed.delete")))
            recreated_state = GuiState(feed_store=other_store)
            recreated = recreated_state.create_stream(
                name="Recreated feed",
                source="external",
                external_url="srt://example.test:9001?mode=caller",
                audit=audit("feed.create"),
            )
            self.assertEqual(recreated.id, self.stream_id)
            self.assertEqual(recreated.config_version, 3)
            with self.assertRaises(OperatorMutationPersistenceError):
                other_state.apply_mode(
                    "video_only",
                    stream_id=self.stream_id,
                    audit=audit("feed.expectation.update"),
                )
            with self.assertRaises(OperatorMutationPersistenceError):
                other_state.update_alert_profile(
                    self.stream_id,
                    ["feed_reachable"],
                    5,
                    audit=audit("feed.alert_profile.update"),
                )
            with self.store._pool.connection() as connection:
                feed_row = connection.execute(
                    """
                    SELECT config, config_version FROM feeds
                    WHERE tenant_id = %s AND id = %s
                    """,
                    (self.tenant_id, self.stream_id),
                ).fetchone()
                generation = connection.execute(
                    """
                    SELECT config_version FROM feed_generations
                    WHERE tenant_id = %s AND id = %s
                    """,
                    (self.tenant_id, self.stream_id),
                ).fetchone()
                actions = connection.execute(
                    """
                    SELECT action FROM audit_events
                    WHERE tenant_id = %s AND resource_id = %s
                    ORDER BY action
                    """,
                    (self.tenant_id, self.stream_id),
                ).fetchall()
            self.assertEqual(feed_row["config"]["name"], "Recreated feed")
            self.assertEqual(feed_row["config_version"], 3)
            self.assertEqual(generation["config_version"], 3)
            self.assertEqual(
                [row["action"] for row in actions], ["feed.create", "feed.delete"]
            )
        finally:
            other_store.close()

    def test_operator_mutation_fails_closed_when_audit_outbox_cannot_commit(self):
        self.enable_trusted_security()
        before = set(self.state.streams)
        allocator_before = self.state._next_stream_number
        with patch.object(
            self.store,
            "_insert_outbox",
            side_effect=PostgresStoreError("audit outbox unavailable"),
        ):
            response = self.post_form(
                "/streams/create",
                {
                    "name": "Must not commit",
                    "source": "generated",
                    "protocol": "srt",
                    "mode": "normal",
                },
                headers=self.operator_headers(),
                expected_status=503,
            )
        self.assertFalse(response["ok"])
        self.assertEqual(set(self.state.streams), before)
        self.assertEqual(self.state._next_stream_number, allocator_before)
        with self.store._pool.connection() as connection:
            feed_row = connection.execute(
                """
                SELECT id FROM feeds
                WHERE tenant_id = %s AND config ->> 'name' = 'Must not commit'
                """,
                (self.tenant_id,),
            ).fetchone()
            audit_row = connection.execute(
                """
                SELECT event_id FROM audit_events
                WHERE tenant_id = %s AND action = 'feed.create'
                  AND payload ->> 'operation' = '/streams/create'
                """,
                (self.tenant_id,),
            ).fetchone()
        self.assertIsNone(feed_row)
        self.assertIsNone(audit_row)

        original_name = self.state.streams[self.stream_id].name
        with patch.object(
            self.store,
            "_insert_outbox",
            side_effect=PostgresStoreError("audit outbox unavailable"),
        ):
            self.post_form(
                "/streams/update",
                {
                    "stream_id": self.stream_id,
                    "name": "Must not update",
                    "source": "generated",
                    "protocol": "srt",
                    "mode": "normal",
                    "framerate": "30",
                },
                headers=self.operator_headers(),
                expected_status=503,
            )
        self.assertEqual(self.state.streams[self.stream_id].name, original_name)
        with self.store._pool.connection() as connection:
            stored = connection.execute(
                """
                SELECT config FROM feeds
                WHERE tenant_id = %s AND id = %s
                """,
                (self.tenant_id, self.stream_id),
            ).fetchone()
        self.assertEqual(stored["config"]["name"], original_name)
        self.assertEqual(stored["config"]["source"], "external")

        with patch.object(
            self.store,
            "_insert_outbox",
            side_effect=PostgresStoreError("audit outbox unavailable"),
        ):
            self.post_form(
                "/streams/delete",
                {"stream_id": self.stream_id},
                headers=self.operator_headers(),
                expected_status=503,
            )
        self.assertIn(self.stream_id, self.state.streams)
        with self.store._pool.connection() as connection:
            self.assertIsNotNone(
                connection.execute(
                    """
                    SELECT id FROM feeds
                    WHERE tenant_id = %s AND id = %s
                    """,
                    (self.tenant_id, self.stream_id),
                ).fetchone()
            )

    def test_operator_auth_denial_is_durable_even_without_mutation(self):
        self.enable_trusted_security()
        sentinel = "query-secret-must-not-persist"
        denied = self.post_form(
            f"/streams/create?access_token={sentinel}",
            {
                "name": "Denied feed",
                "source": "generated",
                "protocol": "srt",
                "mode": "normal",
            },
            headers=self.operator_headers(origin=False),
            expected_status=403,
        )
        self.assertIn("same-origin", denied["error"])
        with self.store._pool.connection() as connection:
            audit_row = connection.execute(
                """
                SELECT event_id, action, outcome, principal_kind,
                       principal_subject, resource_id, payload
                FROM audit_events
                WHERE tenant_id = %s AND action = 'operator_auth'
                ORDER BY occurred_at DESC LIMIT 1
                """,
                (self.tenant_id,),
            ).fetchone()
            outbox_row = connection.execute(
                "SELECT payload FROM outbox WHERE event_id = %s",
                (audit_row["event_id"],),
            ).fetchone()
        self.assertEqual((audit_row["action"], audit_row["outcome"]), ("operator_auth", "denied"))
        self.assertEqual(
            (audit_row["principal_kind"], audit_row["principal_subject"]),
            ("operator", "admin@example.test"),
        )
        self.assertEqual(audit_row["resource_id"], "/streams/create")
        self.assertIn("same-origin", audit_row["payload"]["reason"])
        self.assertNotIn(sentinel, json.dumps(audit_row["payload"]))
        self.assertNotIn(sentinel, json.dumps(outbox_row["payload"]))

        with patch.object(
            self.store,
            "append_audit_event",
            side_effect=PostgresStoreError("audit database unavailable"),
        ):
            still_denied = self.post_form(
                "/streams/create",
                {
                    "name": "Still denied",
                    "source": "generated",
                    "protocol": "srt",
                    "mode": "normal",
                },
                headers=self.operator_headers(origin=False),
                expected_status=403,
            )
        self.assertIn("same-origin", still_denied["error"])
        self.assertNotIn(
            "Still denied", {stream.name for stream in self.state.streams.values()}
        )

    def test_oversized_authenticated_identities_are_rejected_with_durable_denials(self):
        self.enable_trusted_security()
        operator_headers = self.operator_headers()
        operator_headers["X-VideoSim-User"] = "o" * 513
        operator_denied = self.post_form(
            "/streams/create",
            {
                "name": "Oversized denied feed",
                "source": "generated",
                "protocol": "srt",
                "mode": "normal",
            },
            headers=operator_headers,
            expected_status=401,
        )
        self.assertIn("identity exceeds maximum length", operator_denied["error"])
        _, worker_denied = self.get_json(
            "/api/workers/assignments",
            {
                "worker_id": "worker-v2-mtls",
                "worker_incarnation_id": str(uuid.uuid4()),
            },
            expected_status=401,
            headers=self.worker_headers("w" * 513),
        )
        self.assertIn("identity exceeds maximum length", worker_denied["error"])
        with self.store._pool.connection() as connection:
            audits = connection.execute(
                """
                SELECT event_id, action, outcome, principal_kind, principal_subject, payload
                FROM audit_events
                WHERE tenant_id = %s AND action IN ('operator_auth', 'worker_auth')
                ORDER BY action
                """,
                (self.tenant_id,),
            ).fetchall()
            outbox_count = connection.execute(
                """
                SELECT count(*) AS count
                FROM outbox
                WHERE event_id = ANY(%s)
                """,
                ([row["event_id"] for row in audits],),
            ).fetchone()["count"]
        self.assertEqual(
            [
                (row["action"], row["outcome"], row["principal_kind"], row["principal_subject"])
                for row in audits
            ],
            [
                ("operator_auth", "denied", "unknown", "anonymous"),
                ("worker_auth", "denied", "unknown", "anonymous"),
            ],
        )
        self.assertEqual(outbox_count, 2)
        self.assertTrue(
            all("identity exceeds maximum length" in row["payload"]["reason"] for row in audits)
        )

    def test_proxy_auth_denial_is_durable(self):
        self.enable_trusted_security()
        _, denied = self.get_json(
            "/dash/missing/manifest.mpd", {}, expected_status=401
        )
        self.assertIn("trusted proxy", denied["error"])
        with self.store._pool.connection() as connection:
            audit_row = connection.execute(
                """
                SELECT action, outcome, principal_kind, principal_subject,
                       resource_id, payload
                FROM audit_events
                WHERE tenant_id = %s AND action = 'proxy_auth'
                ORDER BY occurred_at DESC LIMIT 1
                """,
                (self.tenant_id,),
            ).fetchone()
        self.assertEqual(
            (
                audit_row["action"],
                audit_row["outcome"],
                audit_row["principal_kind"],
                audit_row["principal_subject"],
            ),
            ("proxy_auth", "denied", "unknown", "anonymous"),
        )
        self.assertEqual(audit_row["resource_id"], "/dash/missing/manifest.mpd")
        self.assertEqual(audit_row["payload"]["method"], "GET")

    def test_offer_ack_report_retry_and_config_fence(self):
        worker_id = "worker-v2-a"
        incarnation = uuid.uuid4()
        assignment = self.assignment(worker_id, incarnation)
        self.assertEqual(assignment["streams"][0]["lease"]["state"], "offered")
        acknowledgement = self.acknowledge(worker_id, incarnation, assignment)
        self.assertEqual(acknowledgement["leases"][0]["state"], "active")
        legacy = self.post_json(
            "/api/workers/report",
            {
                "apiVersion": "videosim.worker/v1",
                "workerId": worker_id,
                "streamIds": [self.stream_id],
                "state": probe_state(self.stream_id),
            },
            expected_status=409,
        )
        self.assertTrue(legacy["retryAssignment"])
        report = self.report_payload(worker_id, incarnation, assignment, 1)

        accepted = self.post_json("/api/workers/report", report)
        duplicate = self.post_json("/api/workers/report", report)
        self.state.update_alert_profile(self.stream_id, ["feed_reachable"], 0)
        stale = self.report_payload(worker_id, incarnation, assignment, 2)
        conflict = self.post_json("/api/workers/report", stale, expected_status=409)

        self.assertTrue(accepted["ok"])
        self.assertEqual(accepted["streamIds"], [self.stream_id])
        self.assertTrue(duplicate["disposition"]["duplicate"])
        self.assertTrue(conflict["retryAssignment"])
        with self.store._pool.connection() as connection:
            result_count = connection.execute(
                "SELECT count(*) AS count FROM check_results WHERE tenant_id = %s AND stream_id = %s",
                (self.tenant_id, self.stream_id),
            ).fetchone()["count"]
        self.assertEqual(result_count, 3)

    def test_worker_drain_fences_old_owner_and_reassigns_immediately(self):
        worker_id = "worker-v2-draining"
        incarnation = uuid.uuid4()
        assignment = self.assignment(worker_id, incarnation)
        self.acknowledge(worker_id, incarnation, assignment)
        self.post_json(
            "/api/workers/report",
            self.report_payload(worker_id, incarnation, assignment, 1),
        )

        drained = self.post_json(
            "/api/workers/drain",
            {
                "apiVersion": "videosim.worker/v2",
                "workerId": worker_id,
                "workerIncarnationId": str(incarnation),
            },
        )

        replacement_id = "worker-v2-drain-replacement"
        replacement_incarnation = uuid.uuid4()
        replacement = self.assignment(replacement_id, replacement_incarnation)
        self.acknowledge(replacement_id, replacement_incarnation, replacement)
        stale = self.post_json(
            "/api/workers/report",
            self.report_payload(worker_id, incarnation, assignment, 2),
            expected_status=409,
        )

        self.assertEqual(drained["drainingStreamIds"], [self.stream_id])
        self.assertGreater(
            replacement["streams"][0]["lease"]["epoch"],
            assignment["streams"][0]["lease"]["epoch"],
        )
        self.assertTrue(stale["retryAssignment"])

    def test_db_projection_is_independent_of_legacy_json_shadow(self):
        self.enable_feed_reachable_alert()
        worker_id = "worker-v2-direct-projection"
        incarnation = uuid.uuid4()
        assignment = self.assignment(worker_id, incarnation)
        self.acknowledge(worker_id, incarnation, assignment)
        report = self.report_payload(
            worker_id,
            incarnation,
            assignment,
            1,
            probe_state(self.stream_id, "issue"),
        )

        with patch("videosim.gui.write_monitor_payload", return_value=False):
            accepted = self.post_json("/api/workers/report", report)
        duplicate = self.post_json("/api/workers/report", report)
        _, state = self.get_json("/state.json", {})

        self.assertTrue(accepted["ok"])
        self.assertTrue(duplicate["disposition"]["duplicate"])
        self.assertEqual(duplicate["projectedStreamIds"], [])
        self.assertFalse(Path(self.state.monitor_state_path).exists())
        self.assertTrue(state["monitor"]["connected"])
        self.assertFalse(state["monitor"]["eventHistoryMutable"])
        self.assertEqual(state["monitor"]["alarms"][0]["monitorId"], "feed_reachable")
        self.assertTrue(state["monitor"]["alarms"][0]["active"])
        self.assertEqual(state["monitor"]["events"][0]["type"], "alarm_raised")
        with self.store._pool.connection() as connection:
            reports = connection.execute(
                "SELECT count(*) AS count FROM worker_reports WHERE report_id = %s",
                (uuid.UUID(report["reportId"]),),
            ).fetchone()["count"]
            results = connection.execute(
                "SELECT count(*) AS count FROM check_results WHERE report_id = %s",
                (uuid.UUID(report["reportId"]),),
            ).fetchone()["count"]
        self.assertEqual((reports, results), (1, 3))

    def test_delayed_duplicate_cannot_overwrite_newer_direct_projection(self):
        self.enable_feed_reachable_alert()
        worker_id = "worker-v2-delayed-retry"
        incarnation = uuid.uuid4()
        assignment = self.assignment(worker_id, incarnation)
        self.acknowledge(worker_id, incarnation, assignment)
        older = self.report_payload(
            worker_id,
            incarnation,
            assignment,
            1,
            probe_state(self.stream_id, "success"),
        )
        self.post_json("/api/workers/report", older)
        newer = self.report_payload(
            worker_id,
            incarnation,
            assignment,
            2,
            probe_state(self.stream_id, "issue"),
        )
        self.post_json("/api/workers/report", newer)

        delayed = self.post_json("/api/workers/report", older)
        _, state = self.get_json("/state.json", {})

        self.assertEqual(delayed["projectedStreamIds"], [])
        alarm = next(
            item for item in state["monitor"]["alarms"]
            if item["monitorId"] == "feed_reachable"
        )
        self.assertTrue(alarm["active"])
        self.assertEqual(alarm["message"], "Feed is unreachable")
        self.assertFalse(Path(self.state.monitor_state_path).exists())

    def test_direct_projection_duplicate_is_noop_after_reassignment(self):
        self.enable_feed_reachable_alert()
        old_worker = "worker-v2-z-direct"
        new_worker = "worker-v2-a-replacement"
        old_incarnation = uuid.uuid4()
        old_assignment = self.assignment(old_worker, old_incarnation)
        self.acknowledge(old_worker, old_incarnation, old_assignment)
        old_report = self.report_payload(
            old_worker,
            old_incarnation,
            old_assignment,
            1,
            probe_state(self.stream_id, "issue"),
        )
        self.post_json("/api/workers/report", old_report)

        new_incarnation = uuid.uuid4()
        replacement = self.assignment(new_worker, new_incarnation)
        self.acknowledge(new_worker, new_incarnation, replacement)
        delayed = self.post_json("/api/workers/report", old_report)

        self.assertEqual(delayed["projectedStreamIds"], [])
        with self.store._pool.connection() as connection:
            alarm = connection.execute(
                """
                SELECT source_result_id FROM current_alarms
                WHERE tenant_id = %s AND stream_id = %s AND monitor_id = 'feed_reachable'
                """,
                (self.tenant_id, self.stream_id),
            ).fetchone()
        self.assertEqual(str(alarm["source_result_id"]), str(uuid.uuid5(
            uuid.UUID(old_report["reportId"]), f"{self.stream_id}:monitor:feed_reachable"
        )))

    def test_direct_projection_duplicate_is_noop_after_expiry_and_reoffer(self):
        self.enable_feed_reachable_alert()
        worker_id = "worker-v2-direct-expiry"
        incarnation = uuid.uuid4()
        assignment = self.assignment(worker_id, incarnation)
        self.acknowledge(worker_id, incarnation, assignment)
        report = self.report_payload(worker_id, incarnation, assignment, 1)
        self.post_json("/api/workers/report", report)
        old_epoch = assignment["streams"][0]["lease"]["epoch"]
        with self.store._pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    UPDATE workers
                    SET last_heartbeat_at = clock_timestamp() - interval '2 minutes'
                    WHERE tenant_id = %s AND worker_id = %s
                    """,
                    (self.tenant_id, worker_id),
                )
                connection.execute(
                    """
                    UPDATE leases SET expires_at = clock_timestamp() - interval '1 second'
                    WHERE tenant_id = %s AND stream_id = %s
                    """,
                    (self.tenant_id, self.stream_id),
                )
        self.post_json(
            "/api/workers/register",
            {
                "apiVersion": "videosim.worker/v2",
                "workerId": worker_id,
                "workerIncarnationId": str(incarnation),
            },
        )
        reoffered = self.assignment(worker_id, incarnation)
        delayed = self.post_json("/api/workers/report", report)

        self.assertGreater(reoffered["streams"][0]["lease"]["epoch"], old_epoch)
        self.assertEqual(delayed["projectedStreamIds"], [])
        self.assertFalse(Path(self.state.monitor_state_path).exists())

    def test_direct_projection_duplicate_is_noop_after_config_revocation(self):
        self.enable_feed_reachable_alert()
        worker_id = "worker-v2-direct-config"
        incarnation = uuid.uuid4()
        assignment = self.assignment(worker_id, incarnation)
        self.acknowledge(worker_id, incarnation, assignment)
        report = self.report_payload(worker_id, incarnation, assignment, 1)
        self.post_json("/api/workers/report", report)

        self.state.update_alert_profile(self.stream_id, ["feed_reachable"], 1)
        delayed = self.post_json("/api/workers/report", report)

        self.assertEqual(delayed["projectedStreamIds"], [])
        self.assertFalse(Path(self.state.monitor_state_path).exists())

    def test_old_incarnation_duplicate_cannot_overwrite_replacement_direct_projection(self):
        self.enable_feed_reachable_alert()
        worker_id = "worker-v2-projection-owner"
        old_incarnation = uuid.uuid4()
        old_assignment = self.assignment(worker_id, old_incarnation)
        self.acknowledge(worker_id, old_incarnation, old_assignment)
        old_report = self.report_payload(
            worker_id,
            old_incarnation,
            old_assignment,
            1,
            probe_state(self.stream_id, "success"),
        )
        self.post_json("/api/workers/report", old_report)
        self.make_worker_stale(worker_id)
        new_incarnation = uuid.uuid4()
        new_assignment = self.assignment(worker_id, new_incarnation)
        self.acknowledge(worker_id, new_incarnation, new_assignment)
        self.post_json(
            "/api/workers/report",
            self.report_payload(
                worker_id,
                new_incarnation,
                new_assignment,
                1,
                probe_state(self.stream_id, "issue"),
            ),
        )

        delayed = self.post_json("/api/workers/report", old_report)
        _, state = self.get_json("/state.json", {})

        self.assertEqual(delayed["projectedStreamIds"], [])
        alarm = next(
            item for item in state["monitor"]["alarms"]
            if item["monitorId"] == "feed_reachable"
        )
        self.assertTrue(alarm["active"])
        self.assertEqual(alarm["message"], "Feed is unreachable")

    def test_concurrent_v2_assignment_polls_partition_streams_once(self):
        second_stream = self.state.create_stream(
            name="Concurrent worker v2 feed",
            source="external",
            external_url="srt://example.test:9002?mode=caller",
        )
        workers = [
            ("worker-v2-concurrent-a", uuid.uuid4()),
            ("worker-v2-concurrent-b", uuid.uuid4()),
        ]
        for worker_id, incarnation in workers:
            self.post_json(
                "/api/workers/register",
                {
                    "apiVersion": "videosim.worker/v2",
                    "workerId": worker_id,
                    "workerIncarnationId": str(incarnation),
                },
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            assignments = list(
                executor.map(
                    lambda worker: self.get_json(
                        "/api/workers/assignments",
                        {
                            "worker_id": worker[0],
                            "worker_incarnation_id": str(worker[1]),
                        },
                    )[1],
                    workers,
                )
            )

        owners = [
            stream["id"] for assignment in assignments for stream in assignment["streams"]
        ]
        self.assertEqual(set(owners), {self.stream_id, second_stream.id})
        self.assertEqual(len(owners), len(set(owners)))
        self.assertTrue(
            all(stream["lease"]["state"] == "offered" for assignment in assignments for stream in assignment["streams"])
        )

    def test_two_worker_join_handoff_stale_owner_and_survivor_pickup(self):
        second_stream = self.state.create_stream(
            name="Worker v2 second feed",
            source="external",
            external_url="srt://example.test:9001?mode=caller",
        )
        worker_a = "worker-v2-rebalance-a"
        worker_b = "worker-v2-rebalance-b"
        incarnation_a = uuid.uuid4()
        incarnation_b = uuid.uuid4()
        assignment_a_before = self.assignment(
            worker_a, incarnation_a, expected_count=2
        )
        self.acknowledge(worker_a, incarnation_a, assignment_a_before)
        assignment_b = self.assignment(worker_b, incarnation_b)
        self.assertEqual(
            [stream["id"] for stream in assignment_b["streams"]],
            [second_stream.id],
        )
        self.acknowledge(worker_b, incarnation_b, assignment_b)
        stale_a = self.report_payload(
            worker_a,
            incarnation_a,
            assignment_a_before,
            1,
            stream_id=second_stream.id,
        )
        stale_response = self.post_json(
            "/api/workers/report", stale_a, expected_status=409
        )

        self.make_worker_stale(worker_a)
        assignment_b_after = self.assignment(
            worker_b, incarnation_b, expected_count=2
        )
        self.acknowledge(worker_b, incarnation_b, assignment_b_after)
        survivor = self.post_json(
            "/api/workers/report",
            self.report_payload(
                worker_b,
                incarnation_b,
                assignment_b_after,
                1,
                stream_id=self.stream_id,
            ),
        )

        self.assertTrue(stale_response["retryAssignment"])
        old_epoch = next(
            stream["lease"]["epoch"]
            for stream in assignment_a_before["streams"]
            if stream["id"] == self.stream_id
        )
        new_epoch = next(
            stream["lease"]["epoch"]
            for stream in assignment_b_after["streams"]
            if stream["id"] == self.stream_id
        )
        self.assertGreater(new_epoch, old_epoch)
        self.assertTrue(survivor["ok"])

    def test_hard_kill_reassigns_only_after_database_ttl_expiry(self):
        self.state.create_stream(
            name="Worker v2 TTL survivor feed",
            source="external",
            external_url="srt://example.test:9003?mode=caller",
        )
        failed_worker = "worker-v2-ttl-failed"
        survivor_worker = "worker-v2-ttl-survivor"
        failed_incarnation = uuid.uuid4()
        survivor_incarnation = uuid.uuid4()
        self.store.worker_freshness_seconds = 1

        with patch("videosim.gui.WORKER_TTL_SECONDS", 1):
            failed_before = self.assignment(
                failed_worker, failed_incarnation, expected_count=2
            )
            self.acknowledge(failed_worker, failed_incarnation, failed_before)
            survivor_before = self.assignment(
                survivor_worker, survivor_incarnation
            )
            self.acknowledge(
                survivor_worker, survivor_incarnation, survivor_before
            )
            survivor_stream_id = survivor_before["streams"][0]["id"]
            failed_stream_id = next(
                stream["id"]
                for stream in failed_before["streams"]
                if stream["id"] != survivor_stream_id
            )
            failed_epoch = next(
                stream["lease"]["epoch"]
                for stream in failed_before["streams"]
                if stream["id"] == failed_stream_id
            )
            survivor_epoch = survivor_before["streams"][0]["lease"]["epoch"]

            started = time.monotonic()
            time.sleep(0.6)
            before_expiry = self.assignment(
                survivor_worker, survivor_incarnation
            )
            self.assertEqual(
                [stream["id"] for stream in before_expiry["streams"]],
                [survivor_stream_id],
            )
            self.assertEqual(
                before_expiry["streams"][0]["lease"]["epoch"], survivor_epoch
            )
            time.sleep(0.6)
            survivor_after = self.assignment(
                survivor_worker, survivor_incarnation, expected_count=2
            )
            elapsed = time.monotonic() - started

        epochs_after = {
            stream["id"]: stream["lease"]["epoch"]
            for stream in survivor_after["streams"]
        }
        stale = self.post_json(
            "/api/workers/report",
            self.report_payload(
                failed_worker,
                failed_incarnation,
                failed_before,
                1,
                stream_id=failed_stream_id,
            ),
            expected_status=409,
        )

        self.assertGreaterEqual(elapsed, 1)
        self.assertLess(elapsed, 3)
        self.assertGreater(epochs_after[failed_stream_id], failed_epoch)
        self.assertEqual(epochs_after[survivor_stream_id], survivor_epoch)
        self.assertTrue(stale["retryAssignment"])

    def test_real_worker_once_completes_v2_register_offer_ack_and_report(self):
        with patch("videosim.worker.run_monitor_once", return_value=probe_state(self.stream_id)):
            code = run_worker(
                self.base_url,
                "worker-v2-real",
                poll_seconds=1,
                repeat_seconds=5,
                history_limit=20,
                srt_host="127.0.0.1",
                once=True,
                heartbeat_seconds=1,
            )

        self.assertEqual(code, 0)
        with self.store._pool.connection() as connection:
            report_count = connection.execute(
                "SELECT count(*) AS count FROM worker_reports WHERE tenant_id = %s AND worker_id = 'worker-v2-real'",
                (self.tenant_id,),
            ).fetchone()["count"]
            lease = connection.execute(
                "SELECT state, last_sequence FROM leases WHERE tenant_id = %s AND stream_id = %s",
                (self.tenant_id, self.stream_id),
            ).fetchone()
        self.assertEqual(report_count, 1)
        self.assertEqual((lease["state"], lease["last_sequence"]), ("active", 1))

    def test_heartbeat_cannot_resurrect_expired_http_lease(self):
        worker_id = "worker-v2-expired"
        incarnation = uuid.uuid4()
        assignment = self.assignment(worker_id, incarnation)
        self.acknowledge(worker_id, incarnation, assignment)
        old_epoch = assignment["streams"][0]["lease"]["epoch"]
        with self.store._pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    UPDATE workers
                    SET last_heartbeat_at = clock_timestamp() - interval '2 minutes'
                    WHERE tenant_id = %s AND worker_id = %s
                    """,
                    (self.tenant_id, worker_id),
                )
                connection.execute(
                    """
                    UPDATE leases SET expires_at = clock_timestamp() - interval '1 second'
                    WHERE tenant_id = %s AND stream_id = %s
                    """,
                    (self.tenant_id, self.stream_id),
                )
        self.post_json(
            "/api/workers/register",
            {
                "apiVersion": "videosim.worker/v2",
                "workerId": worker_id,
                "workerIncarnationId": str(incarnation),
            },
        )
        stale = self.post_json(
            "/api/workers/report",
            self.report_payload(worker_id, incarnation, assignment, 1),
            expected_status=409,
        )
        replacement = self.assignment(worker_id, incarnation)

        self.assertTrue(stale["retryAssignment"])
        self.assertGreater(replacement["streams"][0]["lease"]["epoch"], old_epoch)
        self.assertEqual(replacement["streams"][0]["lease"]["state"], "offered")

    def test_new_incarnation_advances_epoch_and_accepts_sequence_restart(self):
        worker_id = "worker-v2-restart"
        first_incarnation = uuid.uuid4()
        first = self.assignment(worker_id, first_incarnation)
        self.acknowledge(worker_id, first_incarnation, first)
        self.post_json(
            "/api/workers/report",
            self.report_payload(worker_id, first_incarnation, first, 7),
        )

        second_incarnation = uuid.uuid4()
        _, fresh_conflict = self.get_json(
            "/api/workers/assignments",
            {
                "worker_id": worker_id,
                "worker_incarnation_id": str(second_incarnation),
            },
            expected_status=409,
        )
        self.assertTrue(fresh_conflict["retryAssignment"])
        self.make_worker_stale(worker_id)
        second = self.assignment(worker_id, second_incarnation)
        self.acknowledge(worker_id, second_incarnation, second)
        restarted = self.post_json(
            "/api/workers/report",
            self.report_payload(worker_id, second_incarnation, second, 1),
        )

        self.assertGreater(
            second["streams"][0]["lease"]["epoch"],
            first["streams"][0]["lease"]["epoch"],
        )
        self.assertTrue(restarted["ok"])


if __name__ == "__main__":
    unittest.main()
