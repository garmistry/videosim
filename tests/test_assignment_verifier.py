import copy
import json
import os
import unittest
import uuid
from pathlib import Path

from videosim.assignment_verifier import (
    SNAPSHOT_SCHEMA,
    capture_assignment_snapshot,
    verify_assignment_snapshot,
)
from videosim.migrations import PostgresMigrator
from videosim.postgres_store import PostgresControlPlaneStore


ROOT = Path(__file__).resolve().parents[1]
WORKLOAD = json.loads(
    (ROOT / "scale/workloads/f5-1000-candidate.json").read_text(encoding="utf-8")
)
DATABASE_URL = os.environ.get("VIDEOSIM_TEST_POSTGRES_URL", "")


def candidate_snapshot(*, unavailable_zone=""):
    zones = WORKLOAD["placement"]["zones"]
    workers = [
        f"{zone}-worker-{number:02d}"
        for zone in zones
        for number in range(1, 12)
    ]
    streams = {
        protocol: [f"{protocol}-{number:04d}" for number in range(660)]
        for protocol in ("srt", "dash")
    }
    owners = {
        stream_id: workers[index % len(workers)]
        for protocol_streams in streams.values()
        for index, stream_id in enumerate(protocol_streams)
    }
    if unavailable_zone:
        survivors = [worker for worker in workers if not worker.startswith(unavailable_zone)]
        for protocol_streams in streams.values():
            affected = [
                stream_id
                for stream_id in protocol_streams
                if owners[stream_id].startswith(unavailable_zone)
            ]
            for index, stream_id in enumerate(affected):
                owners[stream_id] = survivors[index % len(survivors)]

    incarnation = {worker: str(uuid.uuid5(uuid.NAMESPACE_DNS, worker)) for worker in workers}
    feeds = [
        {
            "streamId": stream_id,
            "protocol": protocol,
            "configVersion": 1,
            "desired": True,
        }
        for protocol, protocol_streams in streams.items()
        for stream_id in protocol_streams
    ]
    return {
        "schemaVersion": SNAPSHOT_SCHEMA,
        "capturedAt": "2026-07-13T12:00:00+00:00",
        "tenantId": "default",
        "workerFreshnessSeconds": 60,
        "feeds": feeds,
        "workers": [
            {
                "workerId": worker,
                "incarnationId": incarnation[worker],
                "state": "active",
                "lastHeartbeatAt": "2026-07-13T11:59:50+00:00",
                "heartbeatFresh": not (
                    unavailable_zone and worker.startswith(unavailable_zone)
                ),
                "capacity": {
                    "maxStreams": 60,
                    "maxSrtStreams": 30,
                    "maxDashStreams": 30,
                    "pressure": {"spoolBlocked": False},
                },
            }
            for worker in workers
        ],
        "leases": [
            {
                "streamId": feed["streamId"],
                "workerId": owners[feed["streamId"]],
                "workerIncarnationId": incarnation[owners[feed["streamId"]]],
                "epoch": 2 if unavailable_zone else 1,
                "configVersion": 1,
                "expiresAt": "2026-07-13T12:01:00+00:00",
                "state": "active",
            }
            for feed in feeds
        ],
    }


class AssignmentVerifierTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.baseline = candidate_snapshot()

    def test_exact_candidate_baseline_and_domain_loss_pass(self):
        baseline_report = verify_assignment_snapshot(self.baseline, WORKLOAD)
        self.assertTrue(baseline_report.passed, baseline_report.errors)
        self.assertEqual(baseline_report.metrics["freshWorkers"], 33)
        self.assertEqual(baseline_report.metrics["authoritativeByProtocol"], {"srt": 660, "dash": 660})

        loss_report = verify_assignment_snapshot(
            candidate_snapshot(unavailable_zone="candidate-zone-a"),
            WORKLOAD,
            self.baseline,
        )
        self.assertTrue(loss_report.passed, loss_report.errors)
        self.assertEqual(loss_report.metrics["freshWorkers"], 22)
        self.assertEqual(loss_report.metrics["ownershipChanges"], 440)
        self.assertTrue(
            all(
                worker["assignedStreams"] == 60
                and worker["srtStreams"] == 30
                and worker["dashStreams"] == 30
                for worker in loss_report.metrics["perWorker"]
            )
        )

    def test_duplicate_authority_fails_closed(self):
        snapshot = copy.deepcopy(self.baseline)
        duplicate = dict(snapshot["leases"][0])
        other = snapshot["workers"][1]
        duplicate["workerId"] = other["workerId"]
        duplicate["workerIncarnationId"] = other["incarnationId"]
        snapshot["leases"].append(duplicate)

        report = verify_assignment_snapshot(snapshot, WORKLOAD)

        self.assertFalse(report.passed)
        self.assertTrue(any("duplicate authority" in error for error in report.errors))

    def test_offered_or_stale_lease_is_not_authority(self):
        snapshot = copy.deepcopy(self.baseline)
        snapshot["leases"][0]["state"] = "offered"

        report = verify_assignment_snapshot(snapshot, WORKLOAD)

        self.assertFalse(report.passed)
        self.assertTrue(any("lack authority" in error for error in report.errors))


@unittest.skipUnless(DATABASE_URL, "VIDEOSIM_TEST_POSTGRES_URL is not configured")
class AssignmentSnapshotPostgresIntegrationTest(unittest.TestCase):
    def test_read_only_snapshot_verifies_live_lease_authority(self):
        PostgresMigrator(DATABASE_URL).apply()
        tenant_id = f"assignment-test-{uuid.uuid4()}"
        store = PostgresControlPlaneStore(DATABASE_URL, tenant_id=tenant_id)
        try:
            with store._pool.connection() as connection:
                connection.execute(
                    "INSERT INTO tenants (id, name) VALUES (%s, %s)",
                    (tenant_id, tenant_id),
                )
                connection.commit()
            feeds = (
                {
                    "id": "srt-1",
                    "name": "SRT",
                    "source": "external",
                    "external_url": "srt://example.test:9000?mode=caller",
                    "protocol": "srt",
                    "mode": "normal",
                },
                {
                    "id": "dash-1",
                    "name": "DASH",
                    "source": "external",
                    "external_url": "https://example.test/manifest.mpd",
                    "protocol": "dash",
                    "mode": "normal",
                },
            )
            workers = ("zone-a-worker-01", "zone-b-worker-01")
            incarnations = {worker: uuid.uuid4() for worker in workers}
            for feed in feeds:
                store.upsert(feed)
            for worker in workers:
                store.register_worker(
                    worker,
                    incarnations[worker],
                    worker,
                    capacity={"maxStreams": 1, "maxSrtStreams": 1, "maxDashStreams": 1},
                )
            for feed, worker in zip(feeds, workers):
                offered = store.reconcile_lease(
                    feed["id"], worker, incarnations[worker], ttl_seconds=60
                )
                store.acknowledge_lease(
                    feed["id"],
                    worker,
                    incarnations[worker],
                    epoch=offered.epoch,
                    config_version=offered.config_version,
                    ttl_seconds=60,
                )

            workload = {
                "schemaVersion": "videosim.scale-workload/v1",
                "loadStreams": 2,
                "failureDomainsUnavailable": 1,
                "protocolMix": {"srtPercent": 50, "dashPercent": 50},
                "placement": {"zones": ["zone-a", "zone-b"]},
                "workerShape": {
                    "count": 2,
                    "failureDomains": 2,
                    "failureDomainWorkerCounts": [1, 1],
                    "maxStreams": 1,
                    "maxSrtStreams": 1,
                    "maxDashStreams": 1,
                },
            }
            report = verify_assignment_snapshot(
                capture_assignment_snapshot(store), workload
            )
            self.assertTrue(report.passed, report.errors)
        finally:
            with store._pool.connection() as connection:
                connection.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
                connection.commit()
            store.close()


if __name__ == "__main__":
    unittest.main()
