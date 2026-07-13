import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

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
DATABASE_APP_URL = os.environ.get("VIDEOSIM_TEST_POSTGRES_APP_URL", "")
DATABASE_PUBLISHER_URL = os.environ.get("VIDEOSIM_TEST_POSTGRES_PUBLISHER_URL", "")
DATABASE_PRUNER_URL = os.environ.get("VIDEOSIM_TEST_POSTGRES_PRUNER_URL", "")
RUNTIME_ROLE_TEST_READY = bool(
    DATABASE_APP_URL
    and DATABASE_PUBLISHER_URL
    and DATABASE_PRUNER_URL
    and shutil.which("psql")
)


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

    def make_worker_stale(self, worker_id):
        with self.store._pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    UPDATE workers
                    SET last_heartbeat_at = clock_timestamp() - interval '2 minutes'
                    WHERE tenant_id = 'default' AND worker_id = %s
                    """,
                    (worker_id,),
                )

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

    def mutation_audit(self, action):
        return {
            "event_id": uuid.uuid4(),
            "principal_kind": "operator",
            "principal_subject": "admin@example.test",
            "action": action,
            "outcome": "succeeded",
            "occurred_at": datetime.now(timezone.utc),
            "payload": {"operation": f"/{action}", "remote": "127.0.0.1"},
        }

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
            for migration in Path(DEFAULT_MIGRATIONS_DIR).glob("*.sql"):
                if migration.name != source.name and not migration.name.endswith(".down.sql"):
                    Path(directory, migration.name).write_text(
                        migration.read_text(encoding="utf-8"), encoding="utf-8"
                    )
            with self.assertRaisesRegex(MigrationError, "checksum or name changed"):
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

    def test_documented_down_migrations_reverse_schema_transactionally(self):
        down_sql = "\n".join(
            Path(DEFAULT_MIGRATIONS_DIR, name).read_text(encoding="utf-8")
            for name in (
                "005_feed_generation_fence.down.sql",
                "004_immutable_audit_outbox.down.sql",
                "003_direct_monitor_projection.down.sql",
                "002_worker_projection_fence.down.sql",
                "001_durable_control_plane.down.sql",
            )
        )
        with self.store._pool.connection() as connection:
            with connection.transaction(force_rollback=True):
                connection.execute(down_sql)
                self.assertIsNone(
                    connection.execute("SELECT to_regclass('public.feeds')").fetchone()["to_regclass"]
                )
                self.assertIsNone(
                    connection.execute(
                        "SELECT to_regclass('public.worker_projection_state')"
                    ).fetchone()["to_regclass"]
                )
                self.assertIsNone(
                    connection.execute(
                        "SELECT to_regclass('public.current_alarm_pending')"
                    ).fetchone()["to_regclass"]
                )
                self.assertIsNone(
                    connection.execute(
                        "SELECT to_regclass('public.feed_generations')"
                    ).fetchone()["to_regclass"]
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
        self.make_worker_stale(worker_id)
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

    def test_preferred_lease_owners_keep_expired_fresh_worker_and_exclude_stale_worker(self):
        feed_id = f"feed-{uuid.uuid4()}"
        self.store.upsert(feed(feed_id))
        worker_id = f"worker-{uuid.uuid4()}"
        incarnation = uuid.uuid4()
        self.store.register_worker(worker_id, incarnation, f"CN={worker_id}")
        self.store.reconcile_lease(feed_id, worker_id, incarnation, ttl_seconds=60)
        with self.store._pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    UPDATE leases
                    SET expires_at = clock_timestamp() - interval '1 second'
                    WHERE tenant_id = %s AND stream_id = %s
                    """,
                    (self.store.tenant_id, feed_id),
                )

        self.assertEqual(self.store.preferred_lease_owners()[feed_id], worker_id)

        self.make_worker_stale(worker_id)

        self.assertNotIn(feed_id, self.store.preferred_lease_owners())

    def test_scheduler_transaction_rolls_back_and_releases_after_connection_loss(self):
        from psycopg import OperationalError

        feed_id = f"scheduler-feed-{uuid.uuid4()}"
        worker_id = f"scheduler-worker-{uuid.uuid4()}"
        incarnation = uuid.uuid4()
        self.store.upsert(feed(feed_id))
        self.store.register_worker(worker_id, incarnation, f"CN={worker_id}")
        replica = PostgresControlPlaneStore(
            DATABASE_URL, min_pool_size=1, max_pool_size=2
        )
        try:
            with self.assertRaises(OperationalError):
                with self.store.assignment_scheduler_transaction() as connection:
                    with self.assertRaisesRegex(
                        PostgresStoreError, "another scheduler transaction"
                    ):
                        with replica.assignment_scheduler_transaction():
                            pass
                    self.store.reconcile_leases(
                        [feed_id],
                        worker_id,
                        incarnation,
                        ttl_seconds=60,
                        _connection=connection,
                    )
                    with replica._pool.connection() as killer:
                        terminated = killer.execute(
                            "SELECT pg_terminate_backend(%s) AS terminated",
                            (connection.info.backend_pid,),
                        ).fetchone()["terminated"]
                    self.assertTrue(terminated)

            self.assertEqual(replica.leases_for_worker(worker_id, incarnation), [])
            with replica.assignment_scheduler_transaction():
                pass
        finally:
            replica.close()

    def test_batch_lease_reconcile_is_ordered_and_atomic(self):
        feed_ids = [f"batch-a-{uuid.uuid4()}", f"batch-b-{uuid.uuid4()}"]
        for feed_id in feed_ids:
            self.store.upsert(feed(feed_id))
        worker_id = f"worker-{uuid.uuid4()}"
        incarnation = uuid.uuid4()
        self.store.register_worker(worker_id, incarnation, f"CN={worker_id}")

        offered = self.store.reconcile_leases(
            reversed(feed_ids), worker_id, incarnation, ttl_seconds=60
        )
        with self.assertRaisesRegex(LeaseConflict, "configuration changed"):
            self.store.acknowledge_leases(
                [
                    (offered[1].stream_id, offered[1].epoch, offered[1].config_version),
                    (offered[0].stream_id, offered[0].epoch, offered[0].config_version + 1),
                ],
                worker_id,
                incarnation,
                ttl_seconds=60,
            )
        self.assertTrue(
            all(
                lease.state == "offered"
                for lease in self.store.leases_for_worker(worker_id, incarnation)
            )
        )
        acknowledged = self.store.acknowledge_leases(
            [
                (lease.stream_id, lease.epoch, lease.config_version)
                for lease in offered
            ],
            worker_id,
            incarnation,
            ttl_seconds=60,
        )
        renewed = self.store.reconcile_leases(
            feed_ids, worker_id, incarnation, ttl_seconds=60
        )
        before = {lease.stream_id: lease for lease in renewed}

        self.assertEqual([lease.stream_id for lease in offered], list(reversed(feed_ids)))
        self.assertEqual(
            [lease.stream_id for lease in acknowledged], list(reversed(feed_ids))
        )
        self.assertTrue(all(lease.state == "active" for lease in renewed))
        with self.assertRaisesRegex(ValueError, "unique"):
            self.store.reconcile_leases(
                [feed_ids[0], feed_ids[0]],
                worker_id,
                incarnation,
                ttl_seconds=60,
            )

        self.store.upsert(feed(feed_ids[0]) | {"name": "Changed"})
        with self.store._pool.connection() as connection:
            revoked_before = connection.execute(
                """
                SELECT epoch, config_version, state FROM leases
                WHERE tenant_id = 'default' AND stream_id = %s
                """,
                (feed_ids[0],),
            ).fetchone()
        with self.assertRaisesRegex(LeaseConflict, "feed does not exist"):
            self.store.reconcile_leases(
                [feed_ids[0], f"zz-missing-{uuid.uuid4()}"],
                worker_id,
                incarnation,
                ttl_seconds=60,
            )

        with self.store._pool.connection() as connection:
            revoked_after = connection.execute(
                """
                SELECT epoch, config_version, state FROM leases
                WHERE tenant_id = 'default' AND stream_id = %s
                """,
                (feed_ids[0],),
            ).fetchone()
        self.assertEqual(dict(revoked_after), dict(revoked_before))
        self.assertEqual(revoked_after["epoch"], before[feed_ids[0]].epoch)
        self.assertEqual(
            revoked_after["config_version"],
            before[feed_ids[0]].config_version,
        )

    def test_1000_stream_lease_batch_is_ordered_and_bounded(self):
        prefix = f"lease-scale-{uuid.uuid4()}-"
        feed_ids = [f"{prefix}{index:04d}" for index in range(1000)]
        worker_id = f"worker-{uuid.uuid4()}"
        incarnation = uuid.uuid4()
        self.store.register_worker(worker_id, incarnation, f"CN={worker_id}")

        class CountingConnection:
            def __init__(self, connection):
                self.connection = connection
                self.execute_count = 0

            def execute(self, *args, **kwargs):
                self.execute_count += 1
                return self.connection.execute(*args, **kwargs)

        try:
            with self.store._pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        """
                        INSERT INTO feeds (tenant_id, id, config)
                        SELECT %s, stream_id, jsonb_build_object('id', stream_id)
                        FROM unnest(%s::text[]) AS stream_id
                        """,
                        (self.store.tenant_id, feed_ids),
                    )
                    counted = CountingConnection(connection)
                    leases = self.store.reconcile_leases(
                        feed_ids,
                        worker_id,
                        incarnation,
                        ttl_seconds=60,
                        _connection=counted,
                    )

            self.assertEqual(counted.execute_count, 2)
            self.assertEqual([lease.stream_id for lease in leases], feed_ids)
            self.assertTrue(all(lease.state == "offered" for lease in leases))
            acknowledged = self.store.acknowledge_leases(
                [
                    (lease.stream_id, lease.epoch, lease.config_version)
                    for lease in leases
                ],
                worker_id,
                incarnation,
                ttl_seconds=60,
            )
            self.assertEqual(
                [lease.stream_id for lease in acknowledged], feed_ids
            )
            self.assertTrue(
                all(lease.state == "active" for lease in acknowledged)
            )
        finally:
            with self.store._pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        "DELETE FROM feeds WHERE tenant_id = %s AND id = ANY(%s::text[])",
                        (self.store.tenant_id, feed_ids),
                    )
                    connection.execute(
                        "DELETE FROM workers WHERE tenant_id = %s AND worker_id = %s",
                        (self.store.tenant_id, worker_id),
                    )

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
        self.make_worker_stale(worker_id)
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

    def test_authenticated_heartbeat_extends_only_current_active_incarnation_leases(self):
        feed_id = f"feed-{uuid.uuid4()}"
        self.store.upsert(feed(feed_id))
        worker_id = f"worker-{uuid.uuid4()}"
        incarnation = uuid.uuid4()
        self.store.register_worker(worker_id, incarnation, f"CN={worker_id}")
        active = self.activate_lease(
            feed_id, worker_id, incarnation, ttl_seconds=1
        )

        self.store.register_worker(worker_id, incarnation, f"CN={worker_id}")
        renewed = self.store.leases_for_worker(worker_id, incarnation)[0]

        self.assertEqual(renewed.epoch, active.epoch)
        self.assertEqual(renewed.state, "active")
        self.assertGreater(renewed.expires_at, active.expires_at)

    def test_drain_is_incarnation_fenced_terminal_and_reassignable(self):
        feed_id = f"feed-{uuid.uuid4()}"
        self.store.upsert(feed(feed_id))
        worker_id = f"worker-{uuid.uuid4()}"
        incarnation = uuid.uuid4()
        self.store.register_worker(worker_id, incarnation, f"CN={worker_id}")
        lease = self.activate_lease(feed_id, worker_id, incarnation)

        drained = self.store.drain_worker(worker_id, incarnation)
        self.store.register_worker(worker_id, incarnation, f"CN={worker_id}")

        self.assertEqual(drained, [feed_id])
        self.assertNotIn(worker_id, self.store.active_worker_ids())
        self.assertFalse(self.store.heartbeat(worker_id, incarnation))
        with self.assertRaisesRegex(LeaseConflict, "drain fence"):
            self.store.drain_worker(worker_id, uuid.uuid4())

        replacement_id = f"worker-{uuid.uuid4()}"
        replacement_incarnation = uuid.uuid4()
        self.store.register_worker(
            replacement_id, replacement_incarnation, f"CN={replacement_id}"
        )
        replacement = self.store.reconcile_lease(
            feed_id, replacement_id, replacement_incarnation, ttl_seconds=60
        )

        self.assertEqual(replacement.state, "offered")
        self.assertGreater(replacement.epoch, lease.epoch)

    def test_heartbeat_cannot_resurrect_expired_active_lease(self):
        feed_id = f"feed-{uuid.uuid4()}"
        version = self.store.upsert(feed(feed_id))
        worker_id = f"worker-{uuid.uuid4()}"
        incarnation = uuid.uuid4()
        self.store.register_worker(worker_id, incarnation, f"CN={worker_id}")
        active = self.activate_lease(feed_id, worker_id, incarnation)
        with self.store._pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    UPDATE leases SET expires_at = clock_timestamp() - interval '1 second'
                    WHERE tenant_id = 'default' AND stream_id = %s
                    """,
                    (feed_id,),
                )
                connection.execute(
                    """
                    UPDATE workers
                    SET last_heartbeat_at = clock_timestamp() - interval '2 minutes'
                    WHERE tenant_id = 'default' AND worker_id = %s
                    """,
                    (worker_id,),
                )
        self.store.register_worker(worker_id, incarnation, f"CN={worker_id}")
        stale_result = CheckResult(
            uuid.uuid4(), feed_id, "feed_reachable", active.epoch, version, 1,
            "healthy", datetime.now(timezone.utc), {},
        )
        rejected = self.store.ingest_report(
            FencedReport(
                uuid.uuid4(), "default", worker_id, incarnation, (stale_result,)
            )
        )
        replacement = self.store.reconcile_lease(
            feed_id, worker_id, incarnation, ttl_seconds=60
        )

        self.assertEqual(rejected.rejected[0]["reason"], "lease_expired")
        self.assertGreater(replacement.epoch, active.epoch)
        self.assertEqual(replacement.state, "offered")

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
        self.make_worker_stale(worker_id)
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
        replacement = uuid.uuid4()
        with self.assertRaisesRegex(LeaseConflict, "still heartbeat-fresh"):
            self.store.register_worker(worker_id, replacement, f"CN={worker_id}")
        self.make_worker_stale(worker_id)
        self.store.register_worker(worker_id, replacement, f"CN={worker_id}")
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

    def test_missing_monitor_observation_does_not_clear_active_alarm(self):
        feed_id = f"feed-{uuid.uuid4()}"
        version = self.store.upsert(feed(feed_id))
        worker_id = f"worker-{uuid.uuid4()}"
        incarnation = uuid.uuid4()
        self.store.register_worker(worker_id, incarnation, f"CN={worker_id}")
        lease = self.activate_lease(feed_id, worker_id, incarnation)
        active = CheckResult(
            uuid.uuid4(), feed_id, "feed_reachable", lease.epoch, version, 1,
            "unhealthy", datetime.now(timezone.utc), {"message": "unreachable"},
        )
        unrelated = CheckResult(
            uuid.uuid4(), feed_id, "probe.validation", lease.epoch, version, 2,
            "healthy", datetime.now(timezone.utc), {"message": "another check passed"},
        )
        self.store.ingest_report(
            FencedReport(uuid.uuid4(), "default", worker_id, incarnation, (active,))
        )
        self.store.ingest_report(
            FencedReport(uuid.uuid4(), "default", worker_id, incarnation, (unrelated,))
        )
        with self.store._pool.connection() as connection:
            alarm = connection.execute(
                """
                SELECT active FROM current_alarms
                WHERE tenant_id = 'default' AND stream_id = %s
                  AND monitor_id = 'feed_reachable'
                """,
                (feed_id,),
            ).fetchone()
        self.assertTrue(alarm["active"])

    def test_catalog_alarm_delay_pending_and_conclusive_lifecycle(self):
        feed_id = f"feed-{uuid.uuid4()}"
        version = self.store.upsert(
            feed(feed_id)
            | {
                "alert_enabled_ids": ["feed_reachable"],
                "alert_delay_seconds": 10,
            }
        )
        worker_id = f"worker-{uuid.uuid4()}"
        incarnation = uuid.uuid4()
        self.store.register_worker(worker_id, incarnation, f"CN={worker_id}")
        lease = self.activate_lease(feed_id, worker_id, incarnation)

        def send(status, sequence, message="monitor evidence"):
            result = CheckResult(
                uuid.uuid4(),
                feed_id,
                "feed_reachable",
                lease.epoch,
                version,
                sequence,
                status,
                datetime.now(timezone.utc),
                {"message": message, "source": "monitor_observation"},
            )
            self.store.ingest_report(
                FencedReport(uuid.uuid4(), "default", worker_id, incarnation, (result,))
            )
            return result

        first = send("unhealthy", 1, "feed unreachable")
        send("timeout", 2, "probe timeout")
        with self.store._pool.connection() as connection:
            pending = connection.execute(
                """
                SELECT source_result_id FROM current_alarm_pending
                WHERE tenant_id = 'default' AND stream_id = %s
                  AND monitor_id = 'feed_reachable'
                """,
                (feed_id,),
            ).fetchone()
            alarm = connection.execute(
                """
                SELECT active FROM current_alarms
                WHERE tenant_id = 'default' AND stream_id = %s
                  AND monitor_id = 'feed_reachable'
                """,
                (feed_id,),
            ).fetchone()
        self.assertEqual(pending["source_result_id"], first.result_id)
        self.assertIsNone(alarm)

        with self.store._pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    UPDATE current_alarm_pending
                    SET first_seen_at = clock_timestamp() - interval '11 seconds'
                    WHERE tenant_id = 'default' AND stream_id = %s
                      AND monitor_id = 'feed_reachable'
                    """,
                    (feed_id,),
                )
        send("unhealthy", 3, "feed still unreachable")
        send("unknown", 4, "inconclusive")
        send("healthy", 5, "feed recovered")

        projection = self.store.monitor_projection_payload()
        alarm = next(
            item
            for item in projection["alarms"]
            if item["streamId"] == feed_id and item["monitorId"] == "feed_reachable"
        )
        events = [
            item["type"]
            for item in projection["events"]
            if item["streamId"] == feed_id and item["monitorId"] == "feed_reachable"
        ]
        self.assertFalse(alarm["active"])
        self.assertEqual(events, ["alarm_raised", "alarm_cleared"])
        self.assertFalse(
            any(
                item["streamId"] == feed_id and item["monitorId"] == "feed_reachable"
                for item in projection["pending"]
            )
        )

    def test_direct_projection_read_uses_one_repeatable_snapshot(self):
        feed_id = f"feed-{uuid.uuid4()}"
        version = self.store.upsert(feed(feed_id))
        worker_id = f"worker-{uuid.uuid4()}"
        incarnation = uuid.uuid4()
        self.store.register_worker(worker_id, incarnation, f"CN={worker_id}")
        lease = self.activate_lease(feed_id, worker_id, incarnation)

        def report(status, sequence):
            result = CheckResult(
                uuid.uuid4(), feed_id, "feed_reachable", lease.epoch, version,
                sequence, status, datetime.now(timezone.utc), {"message": status},
            )
            return FencedReport(uuid.uuid4(), "default", worker_id, incarnation, (result,))

        self.store.ingest_report(report("unhealthy", 1))
        writer_store = PostgresControlPlaneStore(DATABASE_URL)
        original_connection = self.store._pool.connection
        triggered = False

        class WrappedConnection:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, query, params=None):
                nonlocal triggered
                cursor = self.connection.execute(query, params)
                if not triggered and "FROM current_alarms a" in query:
                    triggered = True
                    writer_store.ingest_report(report("healthy", 2))
                return cursor

            def __getattr__(self, name):
                return getattr(self.connection, name)

        class WrappedContext:
            def __init__(self):
                self.context = original_connection()

            def __enter__(self):
                return WrappedConnection(self.context.__enter__())

            def __exit__(self, *args):
                return self.context.__exit__(*args)

        try:
            with patch.object(self.store._pool, "connection", side_effect=WrappedContext):
                projection = self.store.monitor_projection_payload()
        finally:
            writer_store.close()

        alarm = next(
            item
            for item in projection["alarms"]
            if item["streamId"] == feed_id and item["monitorId"] == "feed_reachable"
        )
        transitions = [
            item["type"]
            for item in projection["events"]
            if item["streamId"] == feed_id and item["monitorId"] == "feed_reachable"
        ]
        self.assertTrue(triggered)
        self.assertTrue(alarm["active"])
        self.assertEqual(transitions, ["alarm_raised"])

    def test_profile_disable_suppresses_active_alarm_and_pending_state(self):
        feed_id = f"feed-{uuid.uuid4()}"
        enabled_feed = feed(feed_id) | {
            "alert_enabled_ids": ["feed_reachable"],
            "alert_delay_seconds": 0,
        }
        version = self.store.upsert(enabled_feed)
        worker_id = f"worker-{uuid.uuid4()}"
        incarnation = uuid.uuid4()
        self.store.register_worker(worker_id, incarnation, f"CN={worker_id}")
        lease = self.activate_lease(feed_id, worker_id, incarnation)
        result = CheckResult(
            uuid.uuid4(),
            feed_id,
            "feed_reachable",
            lease.epoch,
            version,
            1,
            "unhealthy",
            datetime.now(timezone.utc),
            {"message": "feed unreachable"},
        )
        self.store.ingest_report(
            FencedReport(uuid.uuid4(), "default", worker_id, incarnation, (result,))
        )

        changed, new_version = self.store.import_feed_if_changed(
            enabled_feed | {"alert_enabled_ids": []}
        )
        projection = self.store.monitor_projection_payload()
        alarm = next(
            item
            for item in projection["alarms"]
            if item["streamId"] == feed_id and item["monitorId"] == "feed_reachable"
        )
        events = [
            item["type"]
            for item in projection["events"]
            if item["streamId"] == feed_id and item["monitorId"] == "feed_reachable"
        ]
        self.assertTrue(changed)
        self.assertGreater(new_version, version)
        self.assertFalse(alarm["active"])
        self.assertEqual(events, ["alarm_raised", "alarm_suppressed"])

    def test_mode_change_suppresses_no_longer_applicable_alarm(self):
        feed_id = f"feed-{uuid.uuid4()}"
        generated = feed(feed_id) | {
            "source": "generated",
            "external_url": "",
            "mode": "normal",
            "alert_enabled_ids": None,
        }
        version = self.store.upsert(generated)
        worker_id = f"worker-{uuid.uuid4()}"
        incarnation = uuid.uuid4()
        self.store.register_worker(worker_id, incarnation, f"CN={worker_id}")
        lease = self.activate_lease(feed_id, worker_id, incarnation)
        result = CheckResult(
            uuid.uuid4(), feed_id, "essence_video_present", lease.epoch,
            version, 1, "unhealthy", datetime.now(timezone.utc),
            {"message": "video absent"},
        )
        self.store.ingest_report(
            FencedReport(uuid.uuid4(), "default", worker_id, incarnation, (result,))
        )
        self.store.import_feed_if_changed(generated | {"mode": "audio_only"})
        projection = self.store.monitor_projection_payload()
        alarm = next(
            item
            for item in projection["alarms"]
            if item["streamId"] == feed_id and item["monitorId"] == "essence_video_present"
        )
        self.assertFalse(alarm["active"])
        self.assertIn(
            "alarm_suppressed",
            [
                item["type"]
                for item in projection["events"]
                if item["streamId"] == feed_id
            ],
        )

    def test_alarm_event_history_is_hard_capped_without_pruning_outbox(self):
        retention_store = PostgresControlPlaneStore(
            DATABASE_URL,
            alarm_repeat_seconds=1,
            alarm_event_history_limit=2,
            alarm_event_retention_seconds=60,
        )
        try:
            feed_id = f"feed-{uuid.uuid4()}"
            version = retention_store.upsert(
                feed(feed_id) | {"alert_enabled_ids": ["feed_reachable"]}
            )
            worker_id = f"worker-{uuid.uuid4()}"
            incarnation = uuid.uuid4()
            retention_store.register_worker(worker_id, incarnation, f"CN={worker_id}")
            offered = retention_store.reconcile_lease(
                feed_id, worker_id, incarnation, ttl_seconds=60
            )
            lease = retention_store.acknowledge_lease(
                feed_id,
                worker_id,
                incarnation,
                epoch=offered.epoch,
                config_version=offered.config_version,
                ttl_seconds=60,
            )

            def send(sequence):
                result = CheckResult(
                    uuid.uuid4(), feed_id, "feed_reachable", lease.epoch,
                    version, sequence, "unhealthy", datetime.now(timezone.utc),
                    {"message": "still unreachable"},
                )
                retention_store.ingest_report(
                    FencedReport(uuid.uuid4(), "default", worker_id, incarnation, (result,))
                )

            send(1)
            for sequence in (2, 3):
                with retention_store._pool.connection() as connection:
                    with connection.transaction():
                        connection.execute(
                            """
                            UPDATE current_alarms
                            SET last_event_at = clock_timestamp() - interval '2 seconds'
                            WHERE tenant_id = 'default' AND stream_id = %s
                              AND monitor_id = 'feed_reachable'
                            """,
                            (feed_id,),
                        )
                send(sequence)
            with retention_store._pool.connection() as connection:
                event_count = connection.execute(
                    """
                    SELECT count(*) AS count FROM alarm_events
                    WHERE tenant_id = 'default' AND stream_id = %s
                    """,
                    (feed_id,),
                ).fetchone()["count"]
                outbox_count = connection.execute(
                    """
                    SELECT count(*) AS count FROM outbox
                    WHERE payload ->> 'tenantId' = 'default'
                      AND payload ->> 'streamId' = %s
                      AND subject = 'videosim.alarms.transition.v1'
                    """,
                    (feed_id,),
                ).fetchone()["count"]
            self.assertEqual(event_count, 2)
            self.assertEqual(outbox_count, 3)
        finally:
            retention_store.close()

    def test_global_pruner_expires_stopped_stream_history_without_outbox_loss(self):
        feed_id = f"feed-{uuid.uuid4()}"
        version = self.store.upsert(feed(feed_id))
        worker_id = f"worker-{uuid.uuid4()}"
        incarnation = uuid.uuid4()
        self.store.register_worker(worker_id, incarnation, f"CN={worker_id}")
        lease = self.activate_lease(feed_id, worker_id, incarnation)
        result = CheckResult(
            uuid.uuid4(), feed_id, "feed_reachable", lease.epoch, version, 1,
            "unhealthy", datetime.now(timezone.utc), {"message": "unreachable"},
        )
        self.store.ingest_report(
            FencedReport(uuid.uuid4(), "default", worker_id, incarnation, (result,))
        )
        with self.store._pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    UPDATE alarm_events
                    SET created_at = clock_timestamp() - interval '8 days'
                    WHERE tenant_id = 'default' AND stream_id = %s
                    """,
                    (feed_id,),
                )
        self.assertEqual(self.store.prune_expired_alarm_events(batch_size=10), 1)
        with self.store._pool.connection() as connection:
            event_count = connection.execute(
                """
                SELECT count(*) AS count FROM alarm_events
                WHERE tenant_id = 'default' AND stream_id = %s
                """,
                (feed_id,),
            ).fetchone()["count"]
            outbox_count = connection.execute(
                """
                SELECT count(*) AS count FROM outbox
                WHERE payload ->> 'tenantId' = 'default'
                  AND payload ->> 'streamId' = %s
                  AND subject = 'videosim.alarms.transition.v1'
                """,
                (feed_id,),
            ).fetchone()["count"]
        self.assertEqual(event_count, 0)
        self.assertEqual(outbox_count, 1)

    def test_probe_metrics_do_not_materialize_operator_alarms(self):
        feed_id = f"feed-{uuid.uuid4()}"
        version = self.store.upsert(feed(feed_id))
        worker_id = f"worker-{uuid.uuid4()}"
        incarnation = uuid.uuid4()
        pressure = {
            "assignedStreams": 12,
            "cycleActive": False,
            "lastValidationDeferred": 3,
        }
        self.store.register_worker(
            worker_id,
            incarnation,
            f"CN={worker_id}",
            capacity={"pressure": pressure},
        )
        lease = self.activate_lease(feed_id, worker_id, incarnation)
        result = CheckResult(
            uuid.uuid4(),
            feed_id,
            "probe.validation",
            lease.epoch,
            version,
            1,
            "unhealthy",
            datetime.now(timezone.utc),
            {"message": "aggregate probe issue"},
        )
        self.store.ingest_report(
            FencedReport(uuid.uuid4(), "default", worker_id, incarnation, (result,))
        )
        projection = self.store.monitor_projection_payload()
        self.assertFalse(
            any(item["streamId"] == feed_id for item in projection["alarms"])
        )
        self.assertEqual(projection["workerProbeMetrics"][worker_id]["streams"][0]["check"], "validation")
        self.assertEqual(projection["workers"][0]["pressure"], pressure)

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
        self.make_worker_stale(worker_id)
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

    def test_feed_mutations_commit_atomically_with_audit_and_outbox(self):
        feed_id = f"feed-{uuid.uuid4()}"
        create_audit = self.mutation_audit("feed.create")
        update_audit = self.mutation_audit("feed.update")
        delete_audit = self.mutation_audit("feed.delete")

        first_version = self.store.upsert_with_audit(feed(feed_id), create_audit)
        second_version = self.store.upsert_with_audit(
            feed(feed_id) | {"name": "Audited update"}, update_audit
        )
        self.store.delete_with_audit(feed_id, delete_audit)

        with self.store._pool.connection() as connection:
            stored_feed = connection.execute(
                "SELECT id FROM feeds WHERE tenant_id = 'default' AND id = %s",
                (feed_id,),
            ).fetchone()
            audit_rows = connection.execute(
                """
                SELECT action, outcome FROM audit_events
                WHERE event_id = ANY(%s::uuid[])
                ORDER BY occurred_at, action
                """,
                ([create_audit["event_id"], update_audit["event_id"], delete_audit["event_id"]],),
            ).fetchall()
            outbox_count = connection.execute(
                """
                SELECT count(*) AS count FROM outbox
                WHERE event_id = ANY(%s::uuid[])
                  AND subject = 'videosim.audit.v1'
                """,
                ([create_audit["event_id"], update_audit["event_id"], delete_audit["event_id"]],),
            ).fetchone()["count"]
        self.assertEqual((first_version, second_version), (1, 2))
        self.assertIsNone(stored_feed)
        self.assertEqual(
            {row["action"] for row in audit_rows},
            {"feed.create", "feed.update", "feed.delete"},
        )
        self.assertTrue(all(row["outcome"] == "succeeded" for row in audit_rows))
        self.assertEqual(outbox_count, 3)

    def test_audit_outbox_failure_rolls_back_feed_create_and_delete(self):
        create_id = f"feed-{uuid.uuid4()}"
        create_audit = self.mutation_audit("feed.create")
        with patch.object(
            self.store,
            "_insert_outbox",
            side_effect=PostgresStoreError("outbox unavailable"),
        ), self.assertRaisesRegex(PostgresStoreError, "outbox unavailable"):
            self.store.upsert_with_audit(feed(create_id), create_audit)
        with self.store._pool.connection() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT id FROM feeds WHERE tenant_id = 'default' AND id = %s",
                    (create_id,),
                ).fetchone()
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT event_id FROM audit_events WHERE event_id = %s",
                    (create_audit["event_id"],),
                ).fetchone()
            )

        delete_id = f"feed-{uuid.uuid4()}"
        self.store.upsert(feed(delete_id))
        delete_audit = self.mutation_audit("feed.delete")
        with patch.object(
            self.store,
            "_insert_outbox",
            side_effect=PostgresStoreError("outbox unavailable"),
        ), self.assertRaisesRegex(PostgresStoreError, "outbox unavailable"):
            self.store.delete_with_audit(delete_id, delete_audit)
        with self.store._pool.connection() as connection:
            self.assertIsNotNone(
                connection.execute(
                    "SELECT id FROM feeds WHERE tenant_id = 'default' AND id = %s",
                    (delete_id,),
                ).fetchone()
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT event_id FROM audit_events WHERE event_id = %s",
                    (delete_audit["event_id"],),
                ).fetchone()
            )
        self.store.delete(delete_id)

    def test_audit_and_outbox_content_are_database_enforced_immutable(self):
        event_id = uuid.uuid4()
        occurred_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        details = {"method": "POST", "operation": "/streams/create"}
        self.store.append_audit_event(
            event_id,
            principal_kind="operator",
            principal_subject="admin@example.test",
            action="feed.create",
            resource_type="feed",
            resource_id="stream-contract",
            outcome="succeeded",
            occurred_at=occurred_at,
            payload=details,
        )
        expected_envelope = {
            "eventId": str(event_id),
            "tenantId": "default",
            "principalKind": "operator",
            "principalSubject": "admin@example.test",
            "action": "feed.create",
            "resourceType": "feed",
            "resourceId": "stream-contract",
            "outcome": "succeeded",
            "occurredAt": occurred_at.isoformat(),
            "details": details,
        }
        with self.store._pool.connection() as connection:
            audit_row = connection.execute(
                "SELECT payload, payload_sha256 FROM audit_events WHERE event_id = %s",
                (event_id,),
            ).fetchone()
            outbox_row = connection.execute(
                "SELECT subject, payload, payload_sha256 FROM outbox WHERE event_id = %s",
                (event_id,),
            ).fetchone()
        self.assertEqual(audit_row["payload"], details)
        self.assertEqual(outbox_row["subject"], "videosim.audit.v1")
        self.assertEqual(outbox_row["payload"], expected_envelope)
        expected_hash = hashlib.sha256(
            json.dumps(expected_envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.assertEqual(audit_row["payload_sha256"], expected_hash)
        self.assertEqual(outbox_row["payload_sha256"], expected_hash)

        statements = (
            ("UPDATE audit_events SET outcome = 'changed' WHERE event_id = %s", "append-only"),
            ("DELETE FROM audit_events WHERE event_id = %s", "append-only"),
            ("UPDATE outbox SET subject = 'changed' WHERE event_id = %s", "immutable"),
            ("UPDATE outbox SET payload = '{}'::jsonb WHERE event_id = %s", "immutable"),
            ("DELETE FROM outbox WHERE event_id = %s", "cannot be deleted"),
        )
        for statement, message in statements:
            with self.subTest(statement=statement), self.store._pool.connection() as connection:
                with self.assertRaisesRegex(Exception, message):
                    with connection.transaction():
                        connection.execute(statement, (event_id,))

        claimed = self.store.claim_outbox(
            "immutability-test", event_ids=[event_id]
        )
        self.assertEqual(len(claimed), 1)
        self.store.mark_outbox_published(
            claimed[0].id, "immutability-test", broker_sequence=123
        )
        with self.store._pool.connection() as connection:
            delivery = connection.execute(
                "SELECT state, broker_sequence FROM outbox WHERE event_id = %s",
                (event_id,),
            ).fetchone()
        self.assertEqual((delivery["state"], delivery["broker_sequence"]), ("published", 123))

    @unittest.skipUnless(
        DATABASE_APP_URL, "VIDEOSIM_TEST_POSTGRES_APP_URL is not configured"
    )
    def test_provisioned_runtime_role_can_operate_without_audit_mutation_rights(self):
        tenant_id = f"runtime-role-{uuid.uuid4()}"
        feed_id = f"runtime-role-feed-{uuid.uuid4()}"
        with self.store._pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    "INSERT INTO tenants (id, name) VALUES (%s, %s)",
                    (tenant_id, "Runtime role test"),
                )
        app_store = PostgresControlPlaneStore(
            DATABASE_APP_URL, tenant_id=tenant_id, min_pool_size=1, max_pool_size=2
        )
        try:
            audit = self.mutation_audit("feed.create")
            self.assertEqual(app_store.upsert_with_audit(feed(feed_id), audit), 1)
            worker_id = f"runtime-role-worker-{uuid.uuid4()}"
            incarnation = uuid.uuid4()
            app_store.register_worker(worker_id, incarnation, worker_id)
            offered = app_store.reconcile_lease(
                feed_id, worker_id, incarnation, ttl_seconds=60
            )
            active = app_store.acknowledge_lease(
                feed_id,
                worker_id,
                incarnation,
                epoch=offered.epoch,
                config_version=offered.config_version,
                ttl_seconds=60,
            )
            result = CheckResult(
                uuid.uuid4(),
                feed_id,
                "feed_reachable",
                active.epoch,
                active.config_version,
                1,
                "healthy",
                datetime.now(timezone.utc),
                {"message": "reachable"},
            )
            disposition = app_store.ingest_report(
                FencedReport(uuid.uuid4(), tenant_id, worker_id, incarnation, (result,))
            )
            self.assertEqual(disposition.accepted_result_ids, (str(result.result_id),))
            projection = app_store.monitor_projection_payload()
            self.assertEqual(projection["workers"][0]["id"], worker_id)
            self.assertTrue(projection["connected"])
            with app_store._pool.connection() as connection:
                privileges = connection.execute(
                    """
                    SELECT
                        has_table_privilege(current_user, 'audit_events', 'INSERT') AS audit_insert,
                        has_table_privilege(current_user, 'audit_events', 'UPDATE') AS audit_update,
                        has_table_privilege(current_user, 'audit_events', 'DELETE') AS audit_delete,
                        has_table_privilege(current_user, 'outbox', 'INSERT') AS outbox_insert,
                        has_table_privilege(current_user, 'outbox', 'UPDATE') AS outbox_update,
                        has_table_privilege(current_user, 'outbox', 'DELETE') AS outbox_delete,
                        has_table_privilege(current_user, 'feed_generations', 'SELECT') AS generation_select,
                        has_table_privilege(current_user, 'feed_generations', 'INSERT') AS generation_insert,
                        has_table_privilege(current_user, 'feed_generations', 'UPDATE') AS generation_update,
                        has_table_privilege(current_user, 'feed_generations', 'DELETE') AS generation_delete,
                        has_table_privilege(current_user, 'schema_migrations', 'SELECT') AS migrations_select
                    """
                ).fetchone()
            self.assertEqual(
                dict(privileges),
                {
                    "audit_insert": True,
                    "audit_update": False,
                    "audit_delete": False,
                    "outbox_insert": True,
                    "outbox_update": False,
                    "outbox_delete": False,
                    "generation_select": True,
                    "generation_insert": True,
                    "generation_update": True,
                    "generation_delete": False,
                    "migrations_select": False,
                },
            )
            with app_store._pool.connection() as connection:
                with self.assertRaisesRegex(Exception, "permission denied"):
                    connection.execute(
                        "DELETE FROM audit_events WHERE event_id = %s",
                        (audit["event_id"],),
                    )
                connection.rollback()
            with app_store._pool.connection() as connection:
                with self.assertRaisesRegex(Exception, "permission denied"):
                    connection.execute(
                        "UPDATE outbox SET state = 'published' WHERE event_id = %s",
                        (audit["event_id"],),
                    )
                connection.rollback()
            with app_store._pool.connection() as connection:
                with self.assertRaisesRegex(Exception, "permission denied"):
                    connection.execute(
                        "DELETE FROM outbox WHERE event_id = %s",
                        (audit["event_id"],),
                    )
                connection.rollback()
        finally:
            app_store.close()

    @unittest.skipUnless(
        RUNTIME_ROLE_TEST_READY,
        "runtime role test URLs or psql are not configured",
    )
    def test_runtime_role_provisioner_rejects_owner_password_reuse(self):
        from psycopg.conninfo import conninfo_to_dict

        owner = conninfo_to_dict(DATABASE_URL)
        publisher = conninfo_to_dict(DATABASE_PUBLISHER_URL)
        pruner = conninfo_to_dict(DATABASE_PRUNER_URL)
        required = {
            "PGHOST": owner.get("host", ""),
            "PGPORT": owner.get("port", ""),
            "PGDATABASE": owner.get("dbname", ""),
            "PGUSER": owner.get("user", ""),
            "PGPASSWORD": owner.get("password", ""),
            "POSTGRES_APP_PASSWORD": owner.get("password", ""),
            "POSTGRES_PUBLISHER_PASSWORD": publisher.get("password", ""),
            "POSTGRES_PRUNER_PASSWORD": pruner.get("password", ""),
        }
        if not all(required.values()):
            self.skipTest("runtime role test URLs must include host/port/database/user/password")
        if len(
            {
                required["POSTGRES_APP_PASSWORD"],
                required["POSTGRES_PUBLISHER_PASSWORD"],
                required["POSTGRES_PRUNER_PASSWORD"],
            }
        ) != 3:
            self.skipTest("test runtime passwords are not distinct")
        script = Path(__file__).resolve().parent.parent / "scripts" / "postgres-runtime-role.sh"
        result = subprocess.run(
            [str(script), "prepare"],
            env=os.environ | required,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("must differ from the PostgreSQL owner password", result.stderr)

    @unittest.skipUnless(
        RUNTIME_ROLE_TEST_READY,
        "runtime role test URLs or psql are not configured",
    )
    def test_auxiliary_runtime_roles_can_only_run_their_service_paths(self):
        tenant_id = f"auxiliary-role-{uuid.uuid4()}"
        event_id = uuid.uuid4()
        with self.store._pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    "INSERT INTO tenants (id, name) VALUES (%s, %s)",
                    (tenant_id, "Auxiliary role test"),
                )
        app_store = PostgresControlPlaneStore(
            DATABASE_APP_URL, tenant_id=tenant_id, min_pool_size=1, max_pool_size=2
        )
        publisher_store = PostgresControlPlaneStore(
            DATABASE_PUBLISHER_URL,
            tenant_id=tenant_id,
            min_pool_size=1,
            max_pool_size=2,
        )
        pruner_store = PostgresControlPlaneStore(
            DATABASE_PRUNER_URL,
            tenant_id=tenant_id,
            min_pool_size=1,
            max_pool_size=2,
        )
        try:
            app_store.append_audit_event(
                event_id,
                principal_kind="operator",
                principal_subject="admin@example.test",
                action="feed.create",
                resource_type="feed",
                resource_id="auxiliary-role-feed",
                outcome="succeeded",
                occurred_at=datetime.now(timezone.utc),
                payload={"operation": "/streams/create"},
            )
            claimed = publisher_store.claim_outbox(
                "auxiliary-role-test", event_ids=[event_id]
            )
            self.assertEqual(len(claimed), 1)
            publisher_store.mark_outbox_published(
                claimed[0].id, "auxiliary-role-test", broker_sequence=1
            )
            self.assertEqual(pruner_store.prune_expired_alarm_events(batch_size=1), 0)
        finally:
            pruner_store.close()
            publisher_store.close()
            app_store.close()

    @unittest.skipUnless(
        RUNTIME_ROLE_TEST_READY,
        "runtime role test URLs or psql are not configured",
    )
    def test_runtime_role_provisioner_converges_privilege_drift(self):
        from psycopg import connect
        from psycopg.conninfo import conninfo_to_dict
        from psycopg.rows import dict_row

        owner = conninfo_to_dict(DATABASE_URL)
        app = conninfo_to_dict(DATABASE_APP_URL)
        publisher = conninfo_to_dict(DATABASE_PUBLISHER_URL)
        pruner = conninfo_to_dict(DATABASE_PRUNER_URL)
        required = {
            "PGHOST": owner.get("host", ""),
            "PGPORT": owner.get("port", ""),
            "PGDATABASE": owner.get("dbname", ""),
            "PGUSER": owner.get("user", ""),
            "PGPASSWORD": owner.get("password", ""),
            "POSTGRES_APP_PASSWORD": app.get("password", ""),
            "POSTGRES_PUBLISHER_PASSWORD": publisher.get("password", ""),
            "POSTGRES_PRUNER_PASSWORD": pruner.get("password", ""),
        }
        if not all(required.values()):
            self.skipTest("runtime role test URLs must include host/port/database/user/password")
        parent_role = "videosim_runtime_role_test_parent"
        member_role = "videosim_runtime_role_test_member"
        script = Path(__file__).resolve().parent.parent / "scripts" / "postgres-runtime-role.sh"
        try:
            with self.store._pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        f"""
                        DO $$
                        BEGIN
                            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{parent_role}') THEN
                                REVOKE {parent_role} FROM videosim_app;
                                DROP ROLE {parent_role};
                            END IF;
                            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{member_role}') THEN
                                REVOKE videosim_app FROM {member_role};
                                DROP ROLE {member_role};
                            END IF;
                        END;
                        $$
                        """
                    )
                    connection.execute(f"CREATE ROLE {parent_role} NOLOGIN")
                    connection.execute(f"CREATE ROLE {member_role} NOLOGIN")
                    connection.execute(f"GRANT {parent_role} TO videosim_app")
                    connection.execute(f"GRANT videosim_app TO {member_role}")
                    connection.execute("GRANT ALL ON TABLE audit_events TO videosim_app")
                    connection.execute("ALTER TABLE audit_events OWNER TO videosim_app")

            environment = os.environ | required
            for mode in ("prepare", "grant"):
                result = subprocess.run(
                    [str(script), mode],
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    f"{mode} failed:\nstdout={result.stdout}\nstderr={result.stderr}",
                )

            with self.store._pool.connection() as connection:
                state = connection.execute(
                    """
                    SELECT
                        pg_get_userbyid(c.relowner) = current_user AS owner_reassigned,
                        NOT EXISTS (
                            SELECT 1
                            FROM pg_auth_members AS membership
                            JOIN pg_roles AS parent ON parent.oid = membership.roleid
                            JOIN pg_roles AS child ON child.oid = membership.member
                            WHERE parent.rolname = %s AND child.rolname = 'videosim_app'
                        ) AS parent_membership_revoked,
                        NOT EXISTS (
                            SELECT 1
                            FROM pg_auth_members AS membership
                            JOIN pg_roles AS parent ON parent.oid = membership.roleid
                            JOIN pg_roles AS child ON child.oid = membership.member
                            WHERE parent.rolname = 'videosim_app' AND child.rolname = %s
                        ) AS member_membership_revoked
                    FROM pg_class AS c
                    WHERE c.oid = 'audit_events'::regclass
                    """,
                    (parent_role, member_role),
                ).fetchone()
            self.assertTrue(state["owner_reassigned"])
            self.assertTrue(state["parent_membership_revoked"])
            self.assertTrue(state["member_membership_revoked"])

            def privileges(url, statement):
                with connect(url, row_factory=dict_row) as connection:
                    return connection.execute(statement).fetchone()

            app_rights = privileges(
                DATABASE_APP_URL,
                """
                SELECT has_table_privilege(current_user, 'audit_events', 'UPDATE') AS audit_update,
                       has_table_privilege(current_user, 'audit_events', 'DELETE') AS audit_delete,
                       has_table_privilege(current_user, 'outbox', 'UPDATE') AS outbox_update,
                       has_table_privilege(current_user, 'outbox', 'DELETE') AS outbox_delete
                """,
            )
            publisher_rights = privileges(
                DATABASE_PUBLISHER_URL,
                """
                SELECT has_table_privilege(current_user, 'outbox', 'SELECT') AS outbox_select,
                       has_table_privilege(current_user, 'outbox', 'UPDATE') AS outbox_update,
                       has_table_privilege(current_user, 'outbox', 'INSERT') AS outbox_insert,
                       has_function_privilege(
                           current_user,
                           'videosim_prune_expired_alarm_events(text, integer, integer)',
                           'EXECUTE'
                       ) AS prune_execute,
                       has_function_privilege(
                           current_user,
                           'videosim_grant_runtime_roles()',
                           'EXECUTE'
                       ) AS role_grant_execute,
                       has_table_privilege(current_user, 'feeds', 'SELECT') AS feeds_select
                """,
            )
            pruner_rights = privileges(
                DATABASE_PRUNER_URL,
                """
                SELECT has_table_privilege(current_user, 'alarm_events', 'SELECT') AS events_select,
                       has_table_privilege(current_user, 'alarm_events', 'DELETE') AS events_delete,
                       has_table_privilege(current_user, 'alarm_events', 'INSERT') AS events_insert,
                       has_function_privilege(
                           current_user,
                           'videosim_prune_expired_alarm_events(text, integer, integer)',
                           'EXECUTE'
                       ) AS prune_execute,
                       has_table_privilege(current_user, 'feeds', 'SELECT') AS feeds_select
                """,
            )
            self.assertEqual(
                app_rights,
                {
                    "audit_update": False,
                    "audit_delete": False,
                    "outbox_update": False,
                    "outbox_delete": False,
                },
            )
            self.assertEqual(
                publisher_rights,
                {
                    "outbox_select": True,
                    "outbox_update": True,
                    "outbox_insert": False,
                    "prune_execute": False,
                    "role_grant_execute": False,
                    "feeds_select": False,
                },
            )
            self.assertEqual(
                pruner_rights,
                {
                    "events_select": False,
                    "events_delete": False,
                    "events_insert": False,
                    "prune_execute": True,
                    "feeds_select": False,
                },
            )
        finally:
            with self.store._pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        "DO $$ BEGIN "
                        "EXECUTE format('ALTER TABLE audit_events OWNER TO %I', current_user); "
                        "END $$"
                    )
                    connection.execute(
                        f"""
                        DO $$
                        BEGIN
                            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{parent_role}') THEN
                                REVOKE {parent_role} FROM videosim_app;
                                DROP ROLE {parent_role};
                            END IF;
                            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{member_role}') THEN
                                REVOKE videosim_app FROM {member_role};
                                DROP ROLE {member_role};
                            END IF;
                        END;
                        $$
                        """
                    )
                    connection.execute("SELECT videosim_grant_runtime_roles()")

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
