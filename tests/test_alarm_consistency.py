import hashlib
import json
import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from videosim.alarm_consistency import capture_alarm_consistency
from videosim.migrations import PostgresMigrator
from videosim.postgres_store import PostgresControlPlaneStore


ROOT = Path(__file__).resolve().parents[1]
WORKLOAD = json.loads(
    (ROOT / "scale/workloads/f5-1000-candidate.json").read_text(encoding="utf-8")
)
DATABASE_URL = os.environ.get("VIDEOSIM_TEST_POSTGRES_URL", "")


@unittest.skipUnless(DATABASE_URL, "VIDEOSIM_TEST_POSTGRES_URL is not configured")
class AlarmConsistencyPostgresIntegrationTest(unittest.TestCase):
    def test_p0_exact_candidate_reconciles_and_rejects_an_unexplained_clear(self):
        PostgresMigrator(DATABASE_URL).apply()
        tenant_id = f"alarm-consistency-{uuid.uuid4()}"
        store = PostgresControlPlaneStore(DATABASE_URL, tenant_id=tenant_id)
        try:
            with store._pool.connection() as connection:
                with connection.transaction(force_rollback=True):
                    streams, result_ids = _seed_candidate(connection, tenant_id)

                    baseline = capture_alarm_consistency(
                        store, WORKLOAD, _connection=connection
                    )
                    self.assertTrue(baseline.passed, baseline.errors)
                    self.assertEqual(baseline.metrics["desiredStreams"], 1320)
                    self.assertEqual(
                        baseline.metrics["observedDesiredStreams"], 1320
                    )
                    self.assertEqual(baseline.metrics["currentAlarms"], 1320)
                    self.assertEqual(baseline.metrics["alarmEvents"], 1320)
                    self.assertTrue(all(check["passed"] for check in baseline.checks))

                    _project_inconclusive_result(
                        connection,
                        tenant_id,
                        streams[0],
                        result_ids[0],
                    )
                    inconclusive = capture_alarm_consistency(
                        store, WORKLOAD, _connection=connection
                    )
                    self.assertTrue(inconclusive.passed, inconclusive.errors)
                    self.assertEqual(inconclusive.metrics["checkResults"], 1321)

                    connection.execute(
                        """
                        UPDATE current_alarms
                        SET active = FALSE, cleared_at = clock_timestamp(),
                            updated_at = clock_timestamp()
                        WHERE tenant_id = %s AND stream_id = %s
                          AND monitor_id = 'feed_reachable'
                        """,
                        (tenant_id, streams[0]),
                    )
                    false_clear = capture_alarm_consistency(
                        store, WORKLOAD, _connection=connection
                    )
                    self.assertFalse(false_clear.passed)
                    projection = next(
                        check
                        for check in false_clear.checks
                        if check["name"] == "alarm-event-projection"
                    )
                    self.assertEqual(projection["violations"], 1)
                    self.assertIn(
                        f"{streams[0]}/feed_reachable", projection["samples"]
                    )
        finally:
            store.close()


def _seed_candidate(connection, tenant_id):
    now = datetime.now(timezone.utc)
    report_id = uuid.uuid4()
    worker_id = "candidate-zone-a-worker-01"
    streams = [f"alarm-stream-{number:04d}" for number in range(1320)]
    result_ids = [
        uuid.uuid5(uuid.NAMESPACE_URL, f"{tenant_id}:{stream_id}:unhealthy")
        for stream_id in streams
    ]
    event_ids = [
        uuid.uuid5(result_id, "alarm-raised") for result_id in result_ids
    ]
    evidence = json.dumps({"message": "endpoint unreachable"})

    connection.execute(
        "INSERT INTO tenants (id, name) VALUES (%s, %s)",
        (tenant_id, tenant_id),
    )
    connection.execute(
        """
        INSERT INTO workers (
            tenant_id, worker_id, incarnation_id, certificate_subject,
            capacity, state
        ) VALUES (%s, %s, %s, %s, '{}'::jsonb, 'active')
        """,
        (tenant_id, worker_id, uuid.uuid4(), worker_id),
    )
    worker_incarnation = connection.execute(
        "SELECT incarnation_id FROM workers WHERE tenant_id = %s AND worker_id = %s",
        (tenant_id, worker_id),
    ).fetchone()["incarnation_id"]
    connection.execute(
        """
        INSERT INTO worker_reports (
            report_id, tenant_id, worker_id, worker_incarnation_id,
            payload_sha256, disposition, committed_at
        ) VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, %s)
        """,
        (report_id, tenant_id, worker_id, worker_incarnation, "f" * 64, now),
    )

    with connection.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO feeds (tenant_id, id, config, config_version)
            VALUES (%s, %s, %s::jsonb, 1)
            """,
            [
                (
                    tenant_id,
                    stream_id,
                    json.dumps(
                        {
                            "source": "external",
                            "protocol": "srt" if number % 2 == 0 else "dash",
                        }
                    ),
                )
                for number, stream_id in enumerate(streams)
            ],
        )
        cursor.executemany(
            """
            INSERT INTO check_results (
                result_id, report_id, tenant_id, stream_id, check_id,
                lease_epoch, config_version, sequence, status, observed_at,
                evidence, payload_sha256
            ) VALUES (%s, %s, %s, %s, 'feed_reachable', 1, 1, 1,
                      'unhealthy', %s, %s::jsonb, %s)
            """,
            [
                (
                    result_id,
                    report_id,
                    tenant_id,
                    stream_id,
                    now,
                    evidence,
                    "a" * 64,
                )
                for stream_id, result_id in zip(streams, result_ids)
            ],
        )
        cursor.executemany(
            """
            INSERT INTO current_check_state (
                tenant_id, stream_id, check_id, result_id, lease_epoch,
                config_version, sequence, status, observed_at, expires_at,
                evidence
            ) VALUES (%s, %s, 'feed_reachable', %s, 1, 1, 1,
                      'unhealthy', %s, %s, %s::jsonb)
            """,
            [
                (
                    tenant_id,
                    stream_id,
                    result_id,
                    now,
                    now + timedelta(minutes=5),
                    evidence,
                )
                for stream_id, result_id in zip(streams, result_ids)
            ],
        )
        cursor.executemany(
            """
            INSERT INTO current_alarms (
                tenant_id, stream_id, monitor_id, active, severity, message,
                source_result_id, lease_epoch, config_version, sequence,
                raised_at, cleared_at, last_event_at, updated_at
            ) VALUES (%s, %s, 'feed_reachable', TRUE, 'critical',
                      'endpoint unreachable', %s, 1, 1, 1, %s, NULL, %s, %s)
            """,
            [
                (tenant_id, stream_id, result_id, now, now, now)
                for stream_id, result_id in zip(streams, result_ids)
            ],
        )

        event_rows = []
        outbox_rows = []
        for stream_id, result_id, event_id in zip(
            streams, result_ids, event_ids
        ):
            payload = {
                "eventId": str(event_id),
                "tenantId": tenant_id,
                "streamId": stream_id,
                "monitorId": "feed_reachable",
                "transition": "raised",
                "resultId": str(result_id),
                "message": "endpoint unreachable",
                "severity": "critical",
            }
            encoded = json.dumps(payload, sort_keys=True)
            event_rows.append(
                (event_id, tenant_id, stream_id, result_id, encoded, now)
            )
            outbox_rows.append((event_id, encoded, _payload_sha256(payload)))
        cursor.executemany(
            """
            INSERT INTO alarm_events (
                event_id, tenant_id, stream_id, monitor_id, transition,
                source_result_id, payload, occurred_at
            ) VALUES (%s, %s, %s, 'feed_reachable', 'raised', %s, %s::jsonb, %s)
            """,
            event_rows,
        )
        cursor.executemany(
            """
            INSERT INTO outbox (event_id, subject, payload, payload_sha256)
            VALUES (%s, 'videosim.alarms.transition.v1', %s::jsonb, %s)
            """,
            outbox_rows,
        )
    return streams, result_ids


def _project_inconclusive_result(
    connection, tenant_id, stream_id, unhealthy_result_id
):
    observed_at = datetime.now(timezone.utc)
    result_id = uuid.uuid5(unhealthy_result_id, "timeout")
    report_id = connection.execute(
        "SELECT report_id FROM check_results WHERE result_id = %s",
        (unhealthy_result_id,),
    ).fetchone()["report_id"]
    connection.execute(
        """
        INSERT INTO check_results (
            result_id, report_id, tenant_id, stream_id, check_id, lease_epoch,
            config_version, sequence, status, observed_at, evidence,
            payload_sha256
        ) VALUES (%s, %s, %s, %s, 'feed_reachable', 1, 1, 2, 'timeout',
                  %s, '{"message":"probe timed out"}'::jsonb, %s)
        """,
        (result_id, report_id, tenant_id, stream_id, observed_at, "b" * 64),
    )
    connection.execute(
        """
        UPDATE current_check_state
        SET result_id = %s, sequence = 2, status = 'timeout', observed_at = %s,
            expires_at = %s, evidence = '{"message":"probe timed out"}'::jsonb
        WHERE tenant_id = %s AND stream_id = %s
          AND check_id = 'feed_reachable'
        """,
        (
            result_id,
            observed_at,
            observed_at + timedelta(minutes=5),
            tenant_id,
            stream_id,
        ),
    )


def _payload_sha256(payload):
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


if __name__ == "__main__":
    unittest.main()
