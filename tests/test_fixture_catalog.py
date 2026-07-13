import os
import unittest
import uuid

from videosim.migrations import PostgresMigrator
from videosim.postgres_store import PostgresControlPlaneStore, PostgresStoreError
from videosim.gui import GuiState, worker_assignments_payload


DATABASE_URL = os.environ.get("VIDEOSIM_TEST_POSTGRES_URL", "")


def feed(feed_id: str) -> dict:
    return {
        "id": feed_id,
        "name": feed_id,
        "source": "external",
        "external_url": "srt://fixture.example.test:9000?mode=caller",
        "desired_state": "running",
        "protocol": "srt",
        "mode": "normal",
        "http_port": 8080,
        "feed_port": 9000,
        "width": 320,
        "height": 180,
        "framerate": "10",
        "dash_dir": f"/tmp/videosim-dash/{feed_id}",
        "alert_enabled_ids": None,
        "alert_delay_seconds": 0,
    }


@unittest.skipUnless(DATABASE_URL, "VIDEOSIM_TEST_POSTGRES_URL is not configured")
class FixtureCatalogPostgresIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        PostgresMigrator(DATABASE_URL).apply()

    def setUp(self):
        self.tenant_id = f"fixture-import-{uuid.uuid4()}"
        bootstrap = PostgresControlPlaneStore(DATABASE_URL)
        try:
            with bootstrap._pool.connection() as connection:
                connection.execute(
                    "INSERT INTO tenants (id, name) VALUES (%s, %s)",
                    (self.tenant_id, self.tenant_id),
                )
                connection.commit()
        finally:
            bootstrap.close()
        self.store = PostgresControlPlaneStore(
            DATABASE_URL, tenant_id=self.tenant_id
        )

    def tearDown(self):
        self.store.close()
        cleanup = PostgresControlPlaneStore(DATABASE_URL)
        try:
            with cleanup._pool.connection() as connection:
                connection.execute(
                    "DELETE FROM tenants WHERE id = %s", (self.tenant_id,)
                )
                connection.commit()
        finally:
            cleanup.close()

    def test_marked_atomic_exclusive_import_is_idempotent(self):
        configs = [feed("fixture-00002"), feed("fixture-00001")]

        self.assertEqual(
            self.store.import_feeds_if_changed(configs, reject_unlisted=True), 2
        )
        self.assertEqual(
            self.store.import_feeds_if_changed(configs, reject_unlisted=True), 0
        )
        self.assertEqual(
            [item["id"] for item in self.store.load()],
            ["fixture-00001", "fixture-00002"],
        )

        incarnation = uuid.uuid4()
        self.store.register_worker(
            "fixture-worker",
            incarnation,
            "CN=fixture-worker",
            capacity={
                "maxStreams": 2,
                "maxSrtStreams": 2,
                "maxDashStreams": 1,
            },
        )
        assignment = worker_assignments_payload(
            GuiState(feed_store=self.store),
            "fixture-worker",
            "https://control.example.test",
            incarnation,
            "CN=fixture-worker",
        )
        self.assertEqual(assignment["capacityShortfall"], 0)
        self.assertEqual(
            [item["id"] for item in assignment["streams"]],
            ["fixture-00001", "fixture-00002"],
        )
        self.assertTrue(
            all(item["endpoint"].startswith("srt://") for item in assignment["streams"])
        )
        self.assertTrue(
            all(item["lease"]["state"] == "offered" for item in assignment["streams"])
        )

        with self.assertRaisesRegex(PostgresStoreError, "unlisted feed"):
            self.store.import_feeds_if_changed(
                [feed("fixture-00001")], reject_unlisted=True
            )
        self.assertEqual(len(self.store.load()), 2)

    def test_marked_batch_error_rolls_back_all_rows(self):
        invalid = feed("fixture-00002") | {"name": object()}

        with self.assertRaises(TypeError):
            self.store.import_feeds_if_changed(
                [feed("fixture-00001"), invalid], reject_unlisted=True
            )

        self.assertEqual(self.store.load(), [])

    def test_duplicate_ids_fail_before_database_mutation(self):
        with self.assertRaisesRegex(ValueError, "must be unique"):
            self.store.import_feeds_if_changed(
                [feed("fixture-00001"), feed("fixture-00001")]
            )
        self.assertEqual(self.store.load(), [])


if __name__ == "__main__":
    unittest.main()
