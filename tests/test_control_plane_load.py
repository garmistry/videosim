import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from videosim.alarm_consistency import run_alarm_consistency
from videosim.control_plane_load import run_control_plane_load
from videosim.migrations import PostgresMigrator
from videosim.postgres_store import PostgresControlPlaneStore


ROOT = Path(__file__).resolve().parents[1]
WORKLOAD_PATH = ROOT / "scale/workloads/f5-1000-candidate.json"
DATABASE_URL = os.environ.get("VIDEOSIM_TEST_POSTGRES_URL", "")


@unittest.skipUnless(DATABASE_URL, "VIDEOSIM_TEST_POSTGRES_URL is not configured")
class ControlPlaneLoadPostgresIntegrationTest(unittest.TestCase):
    def test_p0_exact_candidate_sustains_one_durable_tick_and_retains_evidence(self):
        database_name = f"videosim_load_{uuid.uuid4().hex}"
        database_url = _database_url(DATABASE_URL, database_name)
        _database_command(DATABASE_URL, "CREATE", database_name)
        try:
            PostgresMigrator(database_url).apply()
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory, "control-plane-load.json")
                report = run_control_plane_load(
                    database_url,
                    str(WORKLOAD_PATH),
                    0.001,
                    output_path=str(output),
                )

                self.assertTrue(report.passed, report.errors)
                self.assertTrue(json.loads(output.read_text())["passed"])
                self.assertEqual(report.metrics["desiredStreams"], 1320)
                self.assertEqual(report.metrics["freshWorkers"], 33)
                self.assertEqual(report.metrics["authoritativeLeases"], 1320)
                self.assertEqual(report.metrics["currentCheckStreams"], 1320)
                self.assertEqual(report.metrics["reportsAccepted"], 33)
                self.assertEqual(report.metrics["resultsAccepted"], 2640)
                self.assertEqual(report.metrics["resultOutboxEvents"], 33)
                self.assertEqual(
                    report.metrics["assignmentShape"],
                    {
                        "workers": 33,
                        "minimumStreams": 40,
                        "maximumStreams": 40,
                        "minimumSrtStreams": 20,
                        "maximumSrtStreams": 20,
                        "minimumDashStreams": 20,
                        "maximumDashStreams": 20,
                    },
                )

                store = PostgresControlPlaneStore(
                    database_url, tenant_id=report.tenant_id
                )
                try:
                    with store._pool.connection() as connection:
                        retained = connection.execute(
                            """
                            SELECT
                                (SELECT count(*) FROM feeds WHERE tenant_id = %s)
                                    AS feeds,
                                (SELECT count(*) FROM check_results WHERE tenant_id = %s)
                                    AS results,
                                (SELECT count(*) FROM outbox
                                 WHERE payload->>'tenantId' = %s) AS outbox
                            """,
                            (report.tenant_id, report.tenant_id, report.tenant_id),
                        ).fetchone()
                    self.assertEqual(dict(retained), {"feeds": 1320, "results": 2640, "outbox": 33})
                finally:
                    store.close()

                rejected = run_control_plane_load(
                    database_url,
                    str(WORKLOAD_PATH),
                    0.001,
                    output_path=str(Path(directory, "rejected.json")),
                )
                self.assertFalse(rejected.passed)
                self.assertIn("empty disposable database", rejected.errors[0])
        finally:
            _database_command(DATABASE_URL, "DROP", database_name)

    def test_p0_exact_candidate_recovers_one_domain_and_fences_its_stale_report(self):
        database_name = f"videosim_load_{uuid.uuid4().hex}"
        database_url = _database_url(DATABASE_URL, database_name)
        _database_command(DATABASE_URL, "CREATE", database_name)
        try:
            PostgresMigrator(database_url).apply()
            with tempfile.TemporaryDirectory() as directory:
                workload = json.loads(WORKLOAD_PATH.read_text(encoding="utf-8"))
                workload["eventStorms"] = [
                    {"kind": "worker-domain-loss", "offsetSeconds": 0}
                ]
                workload_path = Path(directory, "workload.json")
                workload_path.write_text(json.dumps(workload), encoding="utf-8")

                report = run_control_plane_load(
                    database_url,
                    str(workload_path),
                    8,
                    tick_seconds=0.05,
                    worker_freshness_seconds=3,
                )

                self.assertTrue(report.passed, report.errors)
                event = report.metrics["events"][0]
                self.assertEqual(event["status"], "recovered")
                self.assertEqual(event["failureDomain"], "candidate-zone-a")
                self.assertEqual(event["failedWorkers"], 11)
                self.assertEqual(event["survivorWorkers"], 22)
                self.assertEqual(event["affectedStreams"], 440)
                self.assertEqual(event["ownershipChanges"], 440)
                self.assertEqual(event["healthyOwnershipChanges"], 0)
                self.assertFalse(event["staleReportAccepted"])
                self.assertIn(
                    "worker_heartbeat_stale", event["staleReportRejectedReasons"]
                )
                self.assertLessEqual(event["authorityRecoverySeconds"]["p99"], 90)
                self.assertGreaterEqual(event["postRecoveryReports"], 22)
                self.assertEqual(
                    event["assignmentShape"],
                    {
                        "workers": 22,
                        "minimumStreams": 60,
                        "maximumStreams": 60,
                        "minimumSrtStreams": 30,
                        "maximumSrtStreams": 30,
                        "minimumDashStreams": 30,
                        "maximumDashStreams": 30,
                    },
                )
                self.assertEqual(report.metrics["freshWorkers"], 22)
                self.assertEqual(report.metrics["authoritativeLeases"], 1320)
                self.assertEqual(report.metrics["currentCheckStreams"], 1320)
                self.assertEqual(report.metrics["reportsAccepted"], 55)
                self.assertEqual(report.metrics["resultsAccepted"], 5280)
                self.assertEqual(report.metrics["resultOutboxEvents"], 55)
        finally:
            _database_command(DATABASE_URL, "DROP", database_name)

    def test_p0_exact_candidate_raises_and_clears_endpoint_fault_storm(self):
        database_name = f"videosim_load_{uuid.uuid4().hex}"
        database_url = _database_url(DATABASE_URL, database_name)
        _database_command(DATABASE_URL, "CREATE", database_name)
        try:
            PostgresMigrator(database_url).apply()
            with tempfile.TemporaryDirectory() as directory:
                workload = json.loads(WORKLOAD_PATH.read_text(encoding="utf-8"))
                workload["eventStorms"] = [
                    {"kind": "endpoint-fault-storm", "offsetSeconds": 0}
                ]
                workload_path = Path(directory, "workload.json")
                workload_path.write_text(json.dumps(workload), encoding="utf-8")

                report = run_control_plane_load(
                    database_url,
                    str(workload_path),
                    0.001,
                )

                self.assertTrue(report.passed, report.errors)
                event = report.metrics["events"][0]
                self.assertEqual(event["status"], "recovered")
                self.assertTrue(event["syntheticControlPlaneOnly"])
                self.assertEqual(event["checkId"], "feed_reachable")
                self.assertEqual(event["affectedStreams"], 1320)
                self.assertEqual(event["workers"], 33)
                self.assertEqual(event["reportsAccepted"], 66)
                self.assertEqual(
                    event["raiseAlarmState"],
                    {
                        "activeAlarms": 1320,
                        "pendingAlarms": 0,
                        "raisedEvents": 1320,
                        "clearedEvents": 0,
                        "alarmOutboxEvents": 1320,
                    },
                )
                self.assertEqual(
                    event["recoveryAlarmState"],
                    {
                        "activeAlarms": 0,
                        "pendingAlarms": 0,
                        "raisedEvents": 1320,
                        "clearedEvents": 1320,
                        "alarmOutboxEvents": 2640,
                    },
                )
                self.assertEqual(report.metrics["reportsAccepted"], 99)
                self.assertEqual(report.metrics["resultsAccepted"], 5280)
                self.assertEqual(report.metrics["resultOutboxEvents"], 99)
                self.assertEqual(report.metrics["activeEndpointAlarms"], 0)
                self.assertEqual(report.metrics["pendingEndpointAlarms"], 0)
                self.assertEqual(report.metrics["endpointAlarmEvents"], 2640)
                self.assertEqual(report.metrics["alarmOutboxEvents"], 2640)

                consistency = run_alarm_consistency(
                    database_url,
                    str(workload_path),
                    tenant_id=report.tenant_id,
                )
                self.assertTrue(consistency.passed, consistency.errors)
                self.assertEqual(consistency.metrics["currentAlarms"], 1320)
                self.assertEqual(consistency.metrics["pendingAlarms"], 0)
                self.assertEqual(consistency.metrics["alarmEvents"], 2640)
                self.assertEqual(consistency.metrics["alarmOutboxEvents"], 2640)
        finally:
            _database_command(DATABASE_URL, "DROP", database_name)


def _database_url(url: str, database_name: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit(
        (parsed.scheme, parsed.netloc, f"/{database_name}", parsed.query, parsed.fragment)
    )


def _database_command(url: str, operation: str, database_name: str):
    import psycopg
    from psycopg import sql

    template = (
        "DROP DATABASE {} WITH (FORCE)"
        if operation == "DROP"
        else "CREATE DATABASE {}"
    )
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(sql.SQL(template).format(sql.Identifier(database_name)))


if __name__ == "__main__":
    unittest.main()
