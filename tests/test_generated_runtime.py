import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from videosim.generated_runtime import (
    GeneratedFeedRuntime,
    connect,
    parse_runtime_feed,
    runtime_lock_id,
    runtime_port_lock_id,
)
from videosim.migrations import PostgresMigrator
from videosim.postgres_store import PostgresControlPlaneStore, ReportConflict


DATABASE_URL = os.environ.get("VIDEOSIM_TEST_POSTGRES_URL", "")


def feed_row(
    feed_id="stream-a",
    *,
    version=1,
    desired_state="running",
    protocol="srt",
    mode="normal",
    port=9000,
):
    return {
        "id": feed_id,
        "config_version": version,
        "config": {
            "id": feed_id,
            "name": feed_id,
            "source": "generated",
            "external_url": "",
            "desired_state": desired_state,
            "protocol": protocol,
            "mode": mode,
            "http_port": 8080,
            "feed_port": port,
            "width": 320,
            "height": 180,
            "framerate": "10",
            "dash_dir": f"/tmp/{feed_id}",
            "alert_enabled_ids": None,
            "alert_delay_seconds": 0,
        },
    }


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeDatabase:
    def __init__(self, rows):
        self.rows = {row["id"]: row for row in rows}
        self.locks = {}

    def connect(self):
        return FakeConnection(self)


class FakeConnection:
    def __init__(self, database):
        self.database = database
        self.autocommit = False
        self.closed = False

    def execute(self, query, params=()):
        normalized = " ".join(query.split())
        if normalized.startswith("SELECT pg_try_advisory_lock"):
            lock_id = params[0]
            owner = self.database.locks.get(lock_id)
            if owner is None or owner is self:
                self.database.locks[lock_id] = self
                return FakeCursor([{"locked": True}])
            return FakeCursor([{"locked": False}])
        if normalized.startswith("SELECT pg_advisory_unlock"):
            lock_id = params[0]
            held = self.database.locks.get(lock_id) is self
            if held:
                self.database.locks.pop(lock_id)
            return FakeCursor([{"unlocked": held}])
        if "FROM feeds" in normalized and "AND id = %s" in normalized:
            row = self.database.rows.get(params[1])
            return FakeCursor([row] if row is not None else [])
        if "FROM feeds" in normalized:
            rows = [
                row
                for row in self.database.rows.values()
                if row["config"].get("source") == "generated"
                and row["config"].get("desired_state") == "running"
            ]
            return FakeCursor(sorted(rows, key=lambda row: row["id"]))
        raise AssertionError(normalized)

    def close(self):
        self.closed = True
        for lock_id, owner in list(self.database.locks.items()):
            if owner is self:
                self.database.locks.pop(lock_id)


class FakeProcess:
    next_pid = 1000

    def __init__(self, command):
        self.command = command
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        self.returncode = None
        self.wait_calls = []

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        self.wait_calls.append(timeout)
        self.returncode = 0
        return 0


class ProcessFactory:
    def __init__(self):
        self.processes = []

    def __call__(self, command, *, start_new_session):
        assert start_new_session
        process = FakeProcess(command)
        self.processes.append(process)
        return process


class FailFirstProcessFactory(ProcessFactory):
    def __call__(self, command, *, start_new_session):
        if not self.processes:
            self.processes.append(None)
            raise OSError("media launcher unavailable")
        return super().__call__(command, start_new_session=start_new_session)


class GeneratedRuntimeTest(unittest.TestCase):
    def test_parses_bounded_runtime_feed_and_rejects_unsafe_input(self):
        parsed = parse_runtime_feed(feed_row(), 9000, 9999)
        self.assertEqual((parsed.id, parsed.feed_port, parsed.framerate), ("stream-a", 9000, "10"))

        unsafe = feed_row("../stream-a")
        with self.assertRaisesRegex(ValueError, "safe path segment"):
            parse_runtime_feed(unsafe, 9000, 9999)
        with self.assertRaisesRegex(ValueError, "outside 9001-9999"):
            parse_runtime_feed(feed_row(port=9000), 9001, 9999)

    def test_reconciles_restart_stop_and_capacity_without_local_duplicates(self):
        database = FakeDatabase([feed_row(), feed_row("stream-b", port=9001)])
        factory = ProcessFactory()
        runtime = GeneratedFeedRuntime(
            database.connect(), max_feeds=1, process_factory=factory
        )

        self.assertEqual(runtime.reconcile(), {"desired": 2, "owned": 1, "capacity": 1})
        self.assertEqual(set(runtime.owned), {"stream-a"})
        first = factory.processes[0]

        database.rows["stream-a"] = feed_row(version=2, mode="video_only")
        with patch("videosim.generated_runtime.os.killpg") as killpg:
            runtime.reconcile()
        self.assertEqual(len(factory.processes), 2)
        self.assertEqual(runtime.owned["stream-a"].feed.config_version, 2)
        self.assertEqual(first.wait_calls, [10])
        killpg.assert_called_once_with(first.pid, 2)

        database.rows["stream-a"] = feed_row(version=3, desired_state="stopped")
        with patch("videosim.generated_runtime.os.killpg"):
            runtime.reconcile()
        self.assertEqual(set(runtime.owned), {"stream-b"})

        with patch("videosim.generated_runtime.os.killpg"):
            runtime.close()
        self.assertFalse(database.locks)

    def test_two_runtimes_fence_feed_and_port_then_take_over_after_close(self):
        database = FakeDatabase([feed_row(), feed_row("stream-b")])
        first_factory = ProcessFactory()
        second_factory = ProcessFactory()
        first = GeneratedFeedRuntime(
            database.connect(), max_feeds=1, runtime_id="first", process_factory=first_factory
        )
        second = GeneratedFeedRuntime(
            database.connect(), max_feeds=2, runtime_id="second", process_factory=second_factory
        )

        first.reconcile()
        second.reconcile()
        self.assertEqual(set(first.owned), {"stream-a"})
        self.assertFalse(second.owned)
        self.assertIsNotNone(
            database.locks.get(runtime_lock_id("default", "stream-a"))
        )
        self.assertIsNotNone(
            database.locks.get(runtime_port_lock_id("default", 9000))
        )

        with patch("videosim.generated_runtime.os.killpg"):
            first.close()
        second.reconcile()
        self.assertEqual(set(second.owned), {"stream-a"})
        with patch("videosim.generated_runtime.os.killpg"):
            second.close()

    def test_one_launch_failure_does_not_stop_other_feed_ownership(self):
        database = FakeDatabase([feed_row(), feed_row("stream-b", port=9001)])
        runtime = GeneratedFeedRuntime(
            database.connect(),
            max_feeds=2,
            process_factory=FailFirstProcessFactory(),
        )

        runtime.reconcile()

        self.assertEqual(set(runtime.owned), {"stream-b"})
        self.assertNotIn(runtime_lock_id("default", "stream-a"), database.locks)
        self.assertNotIn(runtime_port_lock_id("default", 9000), database.locks)
        with patch("videosim.generated_runtime.os.killpg"):
            runtime.close()


@unittest.skipUnless(DATABASE_URL, "VIDEOSIM_TEST_POSTGRES_URL is not configured")
class GeneratedRuntimePostgresIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        PostgresMigrator(DATABASE_URL).apply()

    def setUp(self):
        self.tenant_id = f"generated-runtime-{uuid.uuid4()}"
        self.store = PostgresControlPlaneStore(
            DATABASE_URL, tenant_id=self.tenant_id, min_pool_size=1, max_pool_size=2
        )
        with self.store._pool.connection() as connection:
            connection.execute(
                "INSERT INTO tenants (id, name) VALUES (%s, %s)",
                (self.tenant_id, "Generated runtime test"),
            )
        self.version = self.store.upsert(feed_row()["config"])

    def tearDown(self):
        self.store.close()

    def test_session_lock_allows_one_owner_and_clean_takeover(self):
        first_factory = ProcessFactory()
        second_factory = ProcessFactory()
        first = GeneratedFeedRuntime(
            connect(DATABASE_URL),
            tenant_id=self.tenant_id,
            runtime_id="first",
            process_factory=first_factory,
        )
        second = GeneratedFeedRuntime(
            connect(DATABASE_URL),
            tenant_id=self.tenant_id,
            runtime_id="second",
            process_factory=second_factory,
        )
        try:
            first.reconcile()
            second.reconcile()
            self.assertEqual((len(first.owned), len(second.owned)), (1, 0))

            with patch("videosim.generated_runtime.os.killpg"):
                first.close()
            second.reconcile()
            self.assertEqual(len(second.owned), 1)

            updated = feed_row(version=self.version, mode="video_only")["config"]
            next_version = self.store.upsert_versioned(updated, self.version)
            with patch("videosim.generated_runtime.os.killpg"):
                second.reconcile()
            self.assertEqual(second.owned["stream-a"].feed.config_version, next_version)

            stopped = dict(updated, desired_state="stopped")
            self.store.upsert_versioned(stopped, next_version)
            with patch("videosim.generated_runtime.os.killpg"):
                second.reconcile()
            self.assertFalse(second.owned)
        finally:
            if not first.connection.closed:
                with patch("videosim.generated_runtime.os.killpg"):
                    first.close()
            if not second.connection.closed:
                with patch("videosim.generated_runtime.os.killpg"):
                    second.close()

    def test_two_replicas_allocate_exact_1000_unique_srt_ports(self):
        self.store.delete("stream-a")
        other = PostgresControlPlaneStore(
            DATABASE_URL,
            tenant_id=self.tenant_id,
            min_pool_size=1,
            max_pool_size=6,
        )

        def create(index):
            store = self.store if index % 2 == 0 else other
            config = feed_row(f"allocated-{index:04d}")["config"]
            return store.upsert_generated_srt(
                config,
                expected_version=0,
                port_start=9000,
                port_end=9999,
            )[1]

        try:
            with ThreadPoolExecutor(max_workers=12) as executor:
                ports = list(executor.map(create, range(1000)))
            self.assertEqual(set(ports), set(range(9000, 10000)))
            with self.assertRaisesRegex(ReportConflict, "range is exhausted"):
                create(1000)
            from psycopg.errors import UniqueViolation

            with self.assertRaises(UniqueViolation):
                self.store.upsert(feed_row("forced-duplicate")["config"])
        finally:
            other.close()


if __name__ == "__main__":
    unittest.main()
