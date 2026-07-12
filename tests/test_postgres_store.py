import os
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from videosim.migrations import DEFAULT_MIGRATIONS_DIR, MigrationError, PostgresMigrator
from videosim.postgres_store import (
    CheckResult,
    FencedReport,
    LeaseConflict,
    PostgresControlPlaneStore,
    PostgresStoreError,
    ReportConflict,
)


DATABASE_URL = os.environ.get("VIDEOSIM_TEST_POSTGRES_URL", "")


def feed(feed_id):
    return {
        "id": feed_id,
        "name": "Integration feed",
        "source": "external",
        "external_url": "srt://example.test:9000?mode=caller",
        "protocol": "srt",
        "mode": "normal",
        "http_port": 8080,
        "feed_port": 9000,
        "width": 1280,
        "height": 720,
        "framerate": "30",
        "dash_dir": "/tmp/dash",
        "alert_enabled_ids": None,
        "alert_delay_seconds": 0,
    }


@unittest.skipUnless(DATABASE_URL, "VIDEOSIM_TEST_POSTGRES_URL is not configured")
class PostgresControlPlaneIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        PostgresMigrator(DATABASE_URL).apply()
        cls.store = PostgresControlPlaneStore(DATABASE_URL, min_pool_size=1, max_pool_size=4)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()

    def activate_lease(self, feed_id, worker_id, incarnation, ttl_seconds=60):
        offered = self.store.reconcile_lease(
            feed_id, worker_id, incarnation, ttl_seconds=ttl_seconds
        )
        return self.store.acknowledge_lease(
            feed_id,
            worker_id,
            incarnation,
            epoch=offered.epoch,
            config_version=offered.config_version,
            ttl_seconds=ttl_seconds,
        )

    def test_migrations_are_checksum_stable_and_idempotent(self):
        first = PostgresMigrator(DATABASE_URL).apply()
        second = PostgresMigrator(DATABASE_URL).apply()

        self.assertEqual(first, second)
        self.assertTrue(first)
        self.assertTrue(all(item.applied for item in second))

    def test_applied_migration_checksum_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(DEFAULT_MIGRATIONS_DIR, "001_durable_control_plane.sql")
            Path(directory, source.name).write_text(
                source.read_text(encoding="utf-8") + "\n-- altered\n",
                encoding="utf-8",
            )
            with self.assertRaises(MigrationError):
                PostgresMigrator(DATABASE_URL, directory).apply()
            with self.assertRaises(MigrationError):
                PostgresMigrator(DATABASE_URL, directory).status()

    def test_migration_status_rejects_unknown_database_version(self):
        with self.store._pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO schema_migrations (version, name, checksum)
                    VALUES (999999, 'future_test', %s)
                    """,
                    ("f" * 64,),
                )
        try:
            with self.assertRaises(MigrationError):
                PostgresMigrator(DATABASE_URL).status()
        finally:
            with self.store._pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        "DELETE FROM schema_migrations WHERE version = 999999"
                    )

    def test_documented_down_migration_reverses_schema_transactionally(self):
        down_sql = Path(DEFAULT_MIGRATIONS_DIR, "001_durable_control_plane.down.sql").read_text(encoding="utf-8")
        with self.store._pool.connection() as connection:
            with connection.transaction(force_rollback=True):
                connection.execute(down_sql)
                self.assertIsNone(
                    connection.execute("SELECT to_regclass('public.feeds')").fetchone()["to_regclass"]
                )
        with self.store._pool.connection() as connection:
            self.assertIsNotNone(
                connection.execute("SELECT to_regclass('public.feeds')").fetchone()["to_regclass"]
            )

    def test_feed_repository_increments_configuration_version(self):
        feed_id = f"feed-{uuid.uuid4()}"
        first = self.store.upsert(feed(feed_id))
        changed = feed(feed_id) | {"name": "Changed"}
        second = self.store.upsert_versioned(changed, expected_version=first)

        loaded = next(item for item in self.store.load() if item["id"] == feed_id)
        self.assertEqual(first, 1)
        self.assertEqual(second, 2)
        self.assertEqual(loaded["name"], "Changed")
        self.assertEqual(loaded["config_version"], 2)

    def test_sqlite_cutover_import_is_idempotent(self):
        feed_id = f"feed-{uuid.uuid4()}"
        first_changed, first_version = self.store.import_feed_if_changed(feed(feed_id))
        second_changed, second_version = self.store.import_feed_if_changed(feed(feed_id))
        third_changed, third_version = self.store.import_feed_if_changed(
            feed(feed_id) | {"name": "Imported change"}
        )

        self.assertTrue(first_changed)
        self.assertFalse(second_changed)
        self.assertTrue(third_changed)
        self.assertEqual((first_version, second_version, third_version), (1, 1, 2))

    def test_lease_epoch_advances_for_config_and_worker_incarnation(self):
        feed_id = f"feed-{uuid.uuid4()}"
        self.store.upsert(feed(feed_id))
        worker_id = f"worker-{uuid.uuid4()}"
        first_incarnation = uuid.uuid4()
        self.store.register_worker(worker_id, first_incarnation, f"CN={worker_id}")

        first_offer = self.store.reconcile_lease(
            feed_id, worker_id, first_incarnation, ttl_seconds=60
        )
        self.assertEqual(first_offer.state, "offered")
        first = self.store.acknowledge_lease(
            feed_id,
            worker_id,
            first_incarnation,
            epoch=first_offer.epoch,
            config_version=first_offer.config_version,
            ttl_seconds=60,
        )
        renewed = self.store.reconcile_lease(
            feed_id, worker_id, first_incarnation, ttl_seconds=60
        )
        self.store.upsert(feed(feed_id) | {"name": "Changed"})
        config_changed = self.store.reconcile_lease(
            feed_id, worker_id, first_incarnation, ttl_seconds=60
        )
        second_incarnation = uuid.uuid4()
        self.store.register_worker(worker_id, second_incarnation, f"CN={worker_id}")
        restarted = self.store.reconcile_lease(
            feed_id, worker_id, second_incarnation, ttl_seconds=60
        )

        self.assertEqual(first.state, "active")
        self.assertEqual(first.epoch, renewed.epoch)
        self.assertEqual(renewed.state, "active")
        self.assertGreater(config_changed.epoch, renewed.epoch)
        self.assertEqual(config_changed.state, "offered")
        self.assertGreater(restarted.epoch, config_changed.epoch)
        self.assertEqual(restarted.worker_incarnation_id, second_incarnation)
        self.assertEqual(restarted.state, "offered")

    def test_new_epoch_resets_sequence_for_config_incarnation_expiry_and_restore(self):
        feed_id = f"feed-{uuid.uuid4()}"
        version = self.store.upsert(feed(feed_id))
        worker_id = f"worker-{uuid.uuid4()}"
        incarnation = uuid.uuid4()
        self.store.register_worker(worker_id, incarnation, f"CN={worker_id}")
        lease = self.activate_lease(feed_id, worker_id, incarnation)

        def report(active_lease, sequence):
            result = CheckResult(
                uuid.uuid4(), feed_id, "feed_reachable", active_lease.epoch,
                active_lease.config_version, sequence, "healthy",
                datetime.now(timezone.utc), {},
            )
            return self.store.ingest_report(
                FencedReport(
                    uuid.uuid4(), "default", worker_id,
                    active_lease.worker_incarnation_id, (result,),
                )
            )

        self.assertEqual(len(report(lease, 5).accepted_result_ids), 1)
        same_epoch = self.store.reconcile_lease(
            feed_id, worker_id, incarnation, ttl_seconds=60
        )
        self.assertEqual(same_epoch.epoch, lease.epoch)
        self.assertEqual(report(same_epoch, 1).rejected[0]["reason"], "sequence_not_newer")

        version = self.store.upsert(feed(feed_id) | {"name": "Config epoch"})
        lease = self.activate_lease(feed_id, worker_id, incarnation)
        self.assertEqual(lease.config_version, version)
        self.assertEqual(len(report(lease, 1).accepted_result_ids), 1)

        incarnation = uuid.uuid4()
        self.store.register_worker(worker_id, incarnation, f"CN={worker_id}")
        lease = self.activate_lease(feed_id, worker_id, incarnation)
        self.assertEqual(len(report(lease, 1).accepted_result_ids), 1)

        with self.store._pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    UPDATE leases SET expires_at = clock_timestamp() - interval '1 second'
                    WHERE tenant_id = 'default' AND stream_id = %s
                    """,
                    (feed_id,),
                )
        lease = self.activate_lease(feed_id, worker_id, incarnation)
        self.assertEqual(len(report(lease, 1).accepted_result_ids), 1)

        with self.store._pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    UPDATE leases SET state = 'expired', expires_at = clock_timestamp()
                    WHERE tenant_id = 'default' AND stream_id = %s
                    """,
                    (feed_id,),
                )
        lease = self.activate_lease(feed_id, worker_id, incarnation)
        self.assertEqual(len(report(lease, 1).accepted_result_ids), 1)

    def test_lease_requires_acknowledgement_and_fresh_worker_heartbeat(self):
        feed_id = f"feed-{uuid.uuid4()}"
        version = self.store.upsert(feed(feed_id))
        worker_id = f"worker-{uuid.uuid4()}"
        incarnation = uuid.uuid4()
        self.store.register_worker(worker_id, incarnation, f"CN={worker_id}")
        offered = self.store.reconcile_lease(
            feed_id, worker_id, incarnation, ttl_seconds=60
        )
        result = CheckResult(
            uuid.uuid4(), feed_id, "feed_reachable", offered.epoch, version, 1,
            "healthy", datetime.now(timezone.utc), {},
        )

        before_ack = self.store.ingest_report(
            FencedReport(uuid.uuid4(), "default", worker_id, incarnation, (result,))
        )
        active = self.store.acknowledge_lease(
            feed_id,
            worker_id,
            incarnation,
            epoch=offered.epoch,
            config_version=offered.config_version,
            ttl_seconds=60,
        )
        with self.store._pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    UPDATE workers SET last_heartbeat_at = clock_timestamp() - interval '2 minutes'
                    WHERE tenant_id = 'default' AND worker_id = %s
                    """,
                    (worker_id,),
                )
        stale_report = self.store.ingest_report(
            FencedReport(uuid.uuid4(), "default", worker_id, incarnation, (result,))
        )

        self.assertEqual(offered.state, "offered")
        self.assertEqual(active.state, "active")
        self.assertEqual(before_ack.rejected[0]["reason"], "lease_not_active")
        self.assertEqual(stale_report.rejected[0]["reason"], "worker_heartbeat_stale")
        with self.assertRaisesRegex(LeaseConflict, "heartbeat is stale"):
            self.store.reconcile_lease(feed_id, worker_id, incarnation, ttl_seconds=60)

    def test_unknown_worker_report_returns_typed_conflict(self):
        report = FencedReport(uuid.uuid4(), "default", "missing-worker", uuid.uuid4(), ())

        with self.assertRaisesRegex(ReportConflict, "not registered"):
            self.store.ingest_report(report)

    def test_register_reconcile_race_leaves_old_incarnation_fenced(self):
        feed_id = f"feed-{uuid.uuid4()}"
        self.store.upsert(feed(feed_id))
        worker_id = f"worker-{uuid.uuid4()}"
        old_incarnation = uuid.uuid4()
        new_incarnation = uuid.uuid4()
        self.store.register_worker(worker_id, old_incarnation, f"CN={worker_id}")
        self.activate_lease(feed_id, worker_id, old_incarnation)
        barrier = threading.Barrier(2)

        def register_new():
            barrier.wait(timeout=5)
            self.store.register_worker(worker_id, new_incarnation, f"CN={worker_id}")

        def reconcile_old():
            barrier.wait(timeout=5)
            try:
                return self.store.reconcile_lease(
                    feed_id, worker_id, old_incarnation, ttl_seconds=60
                )
            except LeaseConflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(register_new), executor.submit(reconcile_old)]
            for future in futures:
                future.result(timeout=10)

        with self.store._pool.connection() as connection:
            worker = connection.execute(
                "SELECT incarnation_id FROM workers WHERE tenant_id = 'default' AND worker_id = %s",
                (worker_id,),
            ).fetchone()
            lease = connection.execute(
                "SELECT state FROM leases WHERE tenant_id = 'default' AND stream_id = %s",
                (feed_id,),
            ).fetchone()
        self.assertEqual(worker["incarnation_id"], new_incarnation)
        self.assertEqual(lease["state"], "revoked")

    def test_feed_update_races_acknowledgement_and_ingest_safely(self):
        feed_id = f"feed-{uuid.uuid4()}"
        version = self.store.upsert(feed(feed_id))
        worker_id = f"worker-{uuid.uuid4()}"
        incarnation = uuid.uuid4()
        self.store.register_worker(worker_id, incarnation, f"CN={worker_id}")
        offer = self.store.reconcile_lease(
            feed_id, worker_id, incarnation, ttl_seconds=60
        )
        barrier = threading.Barrier(2)

        def acknowledge():
            barrier.wait(timeout=5)
            try:
                return self.store.acknowledge_lease(
                    feed_id, worker_id, incarnation, epoch=offer.epoch,
                    config_version=offer.config_version, ttl_seconds=60,
                )
            except LeaseConflict:
                return None

        def update_for_ack_race():
            barrier.wait(timeout=5)
            self.store.upsert(feed(feed_id) | {"name": "Ack race"})

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(acknowledge), executor.submit(update_for_ack_race)]
            for future in futures:
                future.result(timeout=10)

        active = self.activate_lease(feed_id, worker_id, incarnation)
        result = CheckResult(
            uuid.uuid4(), feed_id, "feed_reachable", active.epoch,
            active.config_version, 1, "healthy", datetime.now(timezone.utc), {},
        )
        barrier = threading.Barrier(2)

        def ingest():
            barrier.wait(timeout=5)
            return self.store.ingest_report(
                FencedReport(uuid.uuid4(), "default", worker_id, incarnation, (result,))
            )

        def update_for_ingest_race():
            barrier.wait(timeout=5)
            self.store.upsert(feed(feed_id) | {"name": "Ingest race"})

        with ThreadPoolExecutor(max_workers=2) as executor:
            disposition, _ = [
                future.result(timeout=10)
                for future in (
                    executor.submit(ingest),
                    executor.submit(update_for_ingest_race),
                )
            ]
        retry = self.store.ingest_report(
            FencedReport(
                uuid.uuid4(), "default", worker_id, incarnation,
                (CheckResult(
                    uuid.uuid4(), feed_id, "feed_reachable", active.epoch,
                    active.config_version, 2, "healthy", datetime.now(timezone.utc), {},
                ),),
            )
        )

        self.assertIn(len(disposition.accepted_result_ids), {0, 1})
        self.assertEqual(retry.accepted_result_ids, ())
        self.assertIn(
            retry.rejected[0]["reason"],
            {"lease_not_active", "feed_config_version_mismatch"},
        )

    def test_worker_restart_fences_old_incarnation_before_lease_reconciliation(self):
        feed_id = f"feed-{uuid.uuid4()}"
        version = self.store.upsert(feed(feed_id))
        worker_id = f"worker-{uuid.uuid4()}"
        old_incarnation = uuid.uuid4()
        self.store.register_worker(worker_id, old_incarnation, f"CN={worker_id}")
        lease = self.activate_lease(feed_id, worker_id, old_incarnation)
        self.store.register_worker(worker_id, uuid.uuid4(), f"CN={worker_id}")
        result = CheckResult(
            uuid.uuid4(), feed_id, "feed_reachable", lease.epoch, version, 1,
            "unhealthy", datetime.now(timezone.utc), {},
        )

        disposition = self.store.ingest_report(
            FencedReport(uuid.uuid4(), "default", worker_id, old_incarnation, (result,))
        )

        self.assertEqual(disposition.accepted_result_ids, ())
        self.assertEqual(disposition.rejected[0]["reason"], "worker_incarnation_mismatch")

    def test_feed_update_fences_old_config_before_lease_reconciliation(self):
        feed_id = f"feed-{uuid.uuid4()}"
        old_version = self.store.upsert(feed(feed_id))
        worker_id = f"worker-{uuid.uuid4()}"
        incarnation = uuid.uuid4()
        self.store.register_worker(worker_id, incarnation, f"CN={worker_id}")
        lease = self.activate_lease(feed_id, worker_id, incarnation)
        self.store.upsert(feed(feed_id) | {"name": "New config"})
        result = CheckResult(
            uuid.uuid4(), feed_id, "feed_reachable", lease.epoch, old_version, 1,
            "unhealthy", datetime.now(timezone.utc), {},
        )

        disposition = self.store.ingest_report(
            FencedReport(uuid.uuid4(), "default", worker_id, incarnation, (result,))
        )

        self.assertEqual(disposition.accepted_result_ids, ())
        self.assertIn(
            disposition.rejected[0]["reason"],
            {"lease_not_active", "feed_config_version_mismatch"},
        )

    def test_report_ingestion_is_fenced_idempotent_and_emits_alarm_transitions(self):
        feed_id = f"feed-{uuid.uuid4()}"
        config_version = self.store.upsert(feed(feed_id))
        worker_id = f"worker-{uuid.uuid4()}"
        incarnation = uuid.uuid4()
        self.store.register_worker(worker_id, incarnation, f"CN={worker_id}")
        lease = self.activate_lease(feed_id, worker_id, incarnation)
        observed = datetime.now(timezone.utc)
        unhealthy = CheckResult(
            result_id=uuid.uuid4(),
            stream_id=feed_id,
            check_id="feed_reachable",
            lease_epoch=lease.epoch,
            config_version=config_version,
            sequence=1,
            status="unhealthy",
            observed_at=observed,
            evidence={"message": "unreachable", "severity": "critical"},
        )
        healthy_other_check = CheckResult(
            result_id=uuid.uuid4(),
            stream_id=feed_id,
            check_id="video_present",
            lease_epoch=lease.epoch,
            config_version=config_version,
            sequence=1,
            status="healthy",
            observed_at=observed,
            evidence={},
        )
        report = FencedReport(uuid.uuid4(), "default", worker_id, incarnation, (unhealthy, healthy_other_check))

        first = self.store.ingest_report(report)
        duplicate = self.store.ingest_report(report)
        healthy = CheckResult(
            result_id=uuid.uuid4(),
            stream_id=feed_id,
            check_id="feed_reachable",
            lease_epoch=lease.epoch,
            config_version=config_version,
            sequence=2,
            status="healthy",
            observed_at=datetime.now(timezone.utc),
            evidence={"message": "recovered", "severity": "critical"},
        )
        recovered = self.store.ingest_report(
            FencedReport(uuid.uuid4(), "default", worker_id, incarnation, (healthy,))
        )

        self.assertEqual(set(first.accepted_result_ids), {str(unhealthy.result_id), str(healthy_other_check.result_id)})
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(recovered.accepted_result_ids, (str(healthy.result_id),))
        with self.store._pool.connection() as connection:
            alarm = connection.execute(
                "SELECT active FROM current_alarms WHERE tenant_id = 'default' AND stream_id = %s AND monitor_id = 'feed_reachable'",
                (feed_id,),
            ).fetchone()
            transitions = connection.execute(
                "SELECT transition FROM alarm_events WHERE tenant_id = 'default' AND stream_id = %s ORDER BY occurred_at",
                (feed_id,),
            ).fetchall()
        self.assertFalse(alarm["active"])
        self.assertEqual([item["transition"] for item in transitions], ["raised", "cleared"])

        changed_payload = FencedReport(report.report_id, "default", worker_id, incarnation, (healthy,))
        with self.assertRaises(ReportConflict):
            self.store.ingest_report(changed_payload)

    def test_result_ids_are_idempotent_and_sequence_collisions_are_disposed(self):
        feed_id = f"feed-{uuid.uuid4()}"
        version = self.store.upsert(feed(feed_id))
        worker_id = f"worker-{uuid.uuid4()}"
        incarnation = uuid.uuid4()
        self.store.register_worker(worker_id, incarnation, f"CN={worker_id}")
        lease = self.activate_lease(feed_id, worker_id, incarnation)
        result = CheckResult(
            uuid.uuid4(), feed_id, "feed_reachable", lease.epoch, version, 1,
            "healthy", datetime.now(timezone.utc), {"message": "ok"},
        )
        first = self.store.ingest_report(
            FencedReport(uuid.uuid4(), "default", worker_id, incarnation, (result,))
        )
        duplicate = self.store.ingest_report(
            FencedReport(uuid.uuid4(), "default", worker_id, incarnation, (result,))
        )
        drifted = CheckResult(
            result.result_id, feed_id, "feed_reachable", lease.epoch, version, 1,
            "unhealthy", result.observed_at, {"message": "changed"},
        )
        drift = self.store.ingest_report(
            FencedReport(uuid.uuid4(), "default", worker_id, incarnation, (drifted,))
        )
        collision_a = CheckResult(
            uuid.uuid4(), feed_id, "video_present", lease.epoch, version, 2,
            "healthy", datetime.now(timezone.utc), {},
        )
        collision_b = CheckResult(
            uuid.uuid4(), feed_id, "video_present", lease.epoch, version, 2,
            "healthy", datetime.now(timezone.utc), {},
        )
        collision = self.store.ingest_report(
            FencedReport(
                uuid.uuid4(), "default", worker_id, incarnation,
                (collision_a, collision_b),
            )
        )

        self.assertEqual(first.accepted_result_ids, (str(result.result_id),))
        self.assertEqual(duplicate.duplicate_result_ids, (str(result.result_id),))
        self.assertEqual(duplicate.rejected, ())
        self.assertEqual(drift.rejected[0]["reason"], "result_id_payload_mismatch")
        self.assertEqual(len(collision.accepted_result_ids), 1)
        self.assertEqual(collision.rejected[0]["reason"], "check_sequence_conflict")

    def test_observation_clock_and_monotonic_time_are_fenced(self):
        feed_id = f"feed-{uuid.uuid4()}"
        version = self.store.upsert(feed(feed_id))
        worker_id = f"worker-{uuid.uuid4()}"
        incarnation = uuid.uuid4()
        self.store.register_worker(worker_id, incarnation, f"CN={worker_id}")
        lease = self.activate_lease(feed_id, worker_id, incarnation)
        now = datetime.now(timezone.utc)

        def ingest(result):
            return self.store.ingest_report(
                FencedReport(uuid.uuid4(), "default", worker_id, incarnation, (result,))
            )

        future = CheckResult(
            uuid.uuid4(), feed_id, "feed_reachable", lease.epoch, version, 1,
            "healthy", now + timedelta(minutes=5), {},
        )
        old = CheckResult(
            uuid.uuid4(), feed_id, "feed_reachable", lease.epoch, version, 1,
            "healthy", now - timedelta(minutes=5), {},
        )
        accepted = CheckResult(
            uuid.uuid4(), feed_id, "feed_reachable", lease.epoch, version, 1,
            "healthy", now, {},
        )
        reordered = CheckResult(
            uuid.uuid4(), feed_id, "feed_reachable", lease.epoch, version, 2,
            "healthy", now - timedelta(seconds=1), {},
        )

        self.assertEqual(ingest(future).rejected[0]["reason"], "observed_at_in_future")
        self.assertEqual(ingest(old).rejected[0]["reason"], "observation_too_old")
        self.assertEqual(ingest(accepted).accepted_result_ids, (str(accepted.result_id),))
        self.assertEqual(
            ingest(reordered).rejected[0]["reason"],
            "observation_older_than_current",
        )

    def test_inconclusive_results_never_raise_or_clear_alarm(self):
        feed_id = f"feed-{uuid.uuid4()}"
        version = self.store.upsert(feed(feed_id))
        worker_id = f"worker-{uuid.uuid4()}"
        incarnation = uuid.uuid4()
        self.store.register_worker(worker_id, incarnation, f"CN={worker_id}")
        lease = self.activate_lease(feed_id, worker_id, incarnation)
        sequence = 1

        def send(status):
            nonlocal sequence
            result = CheckResult(
                uuid.uuid4(), feed_id, "feed_reachable", lease.epoch, version,
                sequence, status, datetime.now(timezone.utc), {},
            )
            sequence += 1
            return self.store.ingest_report(
                FencedReport(uuid.uuid4(), "default", worker_id, incarnation, (result,))
            )

        send("unhealthy")
        for status in ("unknown", "stale", "skipped", "error", "timeout"):
            self.assertEqual(len(send(status).accepted_result_ids), 1)
            with self.store._pool.connection() as connection:
                alarm = connection.execute(
                    """
                    SELECT active FROM current_alarms
                    WHERE tenant_id = 'default' AND stream_id = %s
                      AND monitor_id = 'feed_reachable'
                    """,
                    (feed_id,),
                ).fetchone()
                transitions = connection.execute(
                    "SELECT count(*) AS count FROM alarm_events WHERE tenant_id = 'default' AND stream_id = %s",
                    (feed_id,),
                ).fetchone()["count"]
            self.assertTrue(alarm["active"])
            self.assertEqual(transitions, 1)
        send("healthy")
        with self.store._pool.connection() as connection:
            final_alarm = connection.execute(
                """
                SELECT active FROM current_alarms
                WHERE tenant_id = 'default' AND stream_id = %s
                  AND monitor_id = 'feed_reachable'
                """,
                (feed_id,),
            ).fetchone()
        self.assertFalse(final_alarm["active"])

    def test_simultaneous_reports_accept_one_authoritative_sequence(self):
        feed_id = f"feed-{uuid.uuid4()}"
        version = self.store.upsert(feed(feed_id))
        worker_id = f"worker-{uuid.uuid4()}"
        incarnation = uuid.uuid4()
        self.store.register_worker(worker_id, incarnation, f"CN={worker_id}")
        lease = self.activate_lease(feed_id, worker_id, incarnation)
        barrier = threading.Barrier(2)

        def submit(check_id):
            result = CheckResult(
                uuid.uuid4(), feed_id, check_id, lease.epoch, version, 1,
                "healthy", datetime.now(timezone.utc), {},
            )
            report = FencedReport(
                uuid.uuid4(), "default", worker_id, incarnation, (result,)
            )
            barrier.wait(timeout=5)
            return self.store.ingest_report(report)

        with ThreadPoolExecutor(max_workers=2) as executor:
            dispositions = list(executor.map(submit, ("check-a", "check-b")))

        self.assertEqual(
            sum(len(item.accepted_result_ids) for item in dispositions),
            1,
        )
        self.assertEqual(sum(len(item.rejected) for item in dispositions), 1)
        self.assertIn(
            next(item for item in dispositions if item.rejected).rejected[0]["reason"],
            {"sequence_not_newer", "observation_older_than_current"},
        )

    def test_stale_epoch_cannot_mutate_current_state_or_outbox(self):
        feed_id = f"feed-{uuid.uuid4()}"
        version = self.store.upsert(feed(feed_id))
        worker_id = f"worker-{uuid.uuid4()}"
        first_incarnation = uuid.uuid4()
        self.store.register_worker(worker_id, first_incarnation, f"CN={worker_id}")
        old_lease = self.activate_lease(feed_id, worker_id, first_incarnation)
        new_incarnation = uuid.uuid4()
        self.store.register_worker(worker_id, new_incarnation, f"CN={worker_id}")
        self.store.reconcile_lease(feed_id, worker_id, new_incarnation, ttl_seconds=60)
        result = CheckResult(
            result_id=uuid.uuid4(),
            stream_id=feed_id,
            check_id="feed_reachable",
            lease_epoch=old_lease.epoch,
            config_version=version,
            sequence=1,
            status="unhealthy",
            observed_at=datetime.now(timezone.utc),
            evidence={},
        )

        disposition = self.store.ingest_report(
            FencedReport(uuid.uuid4(), "default", worker_id, first_incarnation, (result,))
        )

        self.assertEqual(disposition.accepted_result_ids, ())
        self.assertEqual(disposition.rejected[0]["reason"], "worker_incarnation_mismatch")
        with self.store._pool.connection() as connection:
            count = connection.execute("SELECT count(*) AS count FROM check_results WHERE result_id = %s", (result.result_id,)).fetchone()
        self.assertEqual(count["count"], 0)

    def test_audit_event_and_outbox_are_committed_idempotently(self):
        event_id = uuid.uuid4()
        kwargs = {
            "principal_kind": "operator",
            "principal_subject": "admin@example.test",
            "action": "feed.update",
            "resource_type": "feed",
            "resource_id": "stream-1",
            "outcome": "allowed",
            "occurred_at": datetime.now(timezone.utc),
            "payload": {"requestId": "request-1"},
        }

        self.store.append_audit_event(event_id, **kwargs)
        self.store.append_audit_event(event_id, **kwargs)

        with self.store._pool.connection() as connection:
            audit_count = connection.execute(
                "SELECT count(*) AS count FROM audit_events WHERE event_id = %s", (event_id,)
            ).fetchone()["count"]
            outbox_count = connection.execute(
                "SELECT count(*) AS count FROM outbox WHERE event_id = %s", (event_id,)
            ).fetchone()["count"]
        self.assertEqual(audit_count, 1)
        self.assertEqual(outbox_count, 1)
        with self.assertRaises(ReportConflict):
            self.store.append_audit_event(event_id, **(kwargs | {"outcome": "denied"}))

    def test_concurrent_outbox_claims_are_disjoint(self):
        event_ids = [uuid.uuid4() for _ in range(20)]
        for event_id in event_ids:
            self.store.enqueue_outbox(
                event_id, "videosim.audit.v1", {"eventId": str(event_id)}
            )
        barrier = threading.Barrier(2)

        def claim(publisher_id):
            barrier.wait(timeout=5)
            return publisher_id, self.store.claim_outbox(
                publisher_id, 20, event_ids=event_ids
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            claimed = list(executor.map(claim, ("publisher-a", "publisher-b")))

        claimed_ids = [record.event_id for _, records in claimed for record in records]
        self.assertEqual(set(claimed_ids), set(event_ids))
        self.assertEqual(len(claimed_ids), len(set(claimed_ids)))
        for publisher_id, records in claimed:
            for record in records:
                self.store.mark_outbox_published(record.id, publisher_id, record.id)

    def test_inspected_dead_outbox_event_can_be_requeued(self):
        event_id = uuid.uuid4()
        self.store.enqueue_outbox(
            event_id, "videosim.audit.v1", {"eventId": str(event_id)}
        )
        with self.store._pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    "UPDATE outbox SET state = 'dead', attempts = 10 WHERE event_id = %s",
                    (event_id,),
                )

        self.assertTrue(self.store.requeue_dead_outbox(event_id))
        self.assertFalse(self.store.requeue_dead_outbox(event_id))
        with self.store._pool.connection() as connection:
            row = connection.execute(
                "SELECT state, attempts FROM outbox WHERE event_id = %s", (event_id,)
            ).fetchone()
        self.assertEqual((row["state"], row["attempts"]), ("pending", 0))

    def test_outbox_claim_failure_and_publish_marking_are_fenced(self):
        event_id = uuid.uuid4()
        publisher_id = f"publisher-{uuid.uuid4()}"
        payload = {"eventId": str(event_id)}
        self.store.enqueue_outbox(event_id, "videosim.audit.v1", payload)
        self.store.enqueue_outbox(event_id, "videosim.audit.v1", payload)
        with self.assertRaisesRegex(ReportConflict, "different subject or payload"):
            self.store.enqueue_outbox(event_id, "videosim.workers.health.v1", payload)
        with self.assertRaisesRegex(ReportConflict, "different subject or payload"):
            self.store.enqueue_outbox(event_id, "videosim.audit.v1", {"eventId": str(event_id), "changed": True})

        claimed = self.store.claim_outbox(publisher_id, 1, event_ids=[event_id])
        self.assertEqual(len(claimed), 1)
        self.store.mark_outbox_failed(claimed[0].id, publisher_id, "broker unavailable")
        with self.store._pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    "UPDATE outbox SET next_attempt_at = clock_timestamp() WHERE id = %s",
                    (claimed[0].id,),
                )
        reclaimed = self.store.claim_outbox(publisher_id, 1, event_ids=[event_id])
        self.assertEqual(reclaimed[0].id, claimed[0].id)
        with self.assertRaisesRegex(PostgresStoreError, "claim is no longer owned"):
            self.store.mark_outbox_published(reclaimed[0].id, "other-publisher", 41)
        self.store.mark_outbox_published(reclaimed[0].id, publisher_id, 42)

        with self.store._pool.connection() as connection:
            row = connection.execute("SELECT state, broker_sequence FROM outbox WHERE event_id = %s", (event_id,)).fetchone()
        self.assertEqual(row["state"], "published")
        self.assertEqual(row["broker_sequence"], 42)


if __name__ == "__main__":
    unittest.main()
