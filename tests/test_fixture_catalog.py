import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from videosim.cli import main
from videosim.fixture_catalog import (
    load_fixture_feed_configs,
    run_fixture_catalog_import,
)
from videosim.fixture_fleet import (
    fixture_state,
    load_fixture_manifest,
    write_fixture_state,
)
from videosim.fixture_scenario import run_fixture_scenario
from videosim.gui import GuiState, worker_assignments_payload
from videosim.migrations import PostgresMigrator
from videosim.postgres_store import PostgresControlPlaneStore, PostgresStoreError


DATABASE_URL = os.environ.get("VIDEOSIM_TEST_POSTGRES_URL", "")
ROOT = Path(__file__).resolve().parents[1]


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


def scenario_state() -> dict:
    streams = []
    for index, protocol in enumerate(("srt", "dash"), start=1):
        endpoint = (
            f"srt://fixture.example.test:{9000 + index}?mode=caller"
            if protocol == "srt"
            else f"https://fixture.example.test/{index}/manifest.mpd"
        )
        streams.append(
            {
                "id": f"fixture-{index:05d}",
                "name": f"Fixture {index:05d}",
                "protocol": protocol,
                "source": "external",
                "mode": "normal",
                "status": "running",
                "endpoint": endpoint,
                "fixtureBehavior": "healthy",
                "fixtureEndpointShared": False,
                "width": 320,
                "height": 180,
                "framerate": "10",
            }
        )
    return {
        "schemaVersion": "videosim.fixture-scenario-state/v1",
        "logicalStreamsShareEndpoints": False,
        "protocolCounts": {"srt": 1, "dash": 1},
        "behaviorCounts": {
            "healthy": 2,
            "slow": 0,
            "dead": 0,
            "malformed": 0,
        },
        "streams": streams,
    }


class FixtureCatalogCliTest(unittest.TestCase):
    def test_cli_delegates_exclusive_distinct_import(self):
        with patch(
            "videosim.cli.run_fixture_catalog_import", return_value={"passed": True}
        ) as run:
            code = main(
                [
                    "import-fixture-scenario",
                    "--database-url",
                    "postgresql://fixture",
                    "--state",
                    "scenario.json",
                    "--output",
                    "import.json",
                ]
            )

        self.assertEqual(code, 0)
        run.assert_called_once_with(
            "postgresql://fixture",
            "scenario.json",
            "import.json",
            require_distinct_endpoints=True,
        )


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

    def test_marked_import_report_matches_persisted_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory, "scenario.json")
            output_path = Path(directory, "import.json")
            state_path.write_text(json.dumps(scenario_state()), encoding="utf-8")
            imported_store = PostgresControlPlaneStore(
                DATABASE_URL, tenant_id=self.tenant_id
            )
            with patch("builtins.print"):
                report = run_fixture_catalog_import(
                    DATABASE_URL,
                    state_path,
                    output_path,
                    store_factory=lambda _url: imported_store,
                )
            persisted_report = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(report, persisted_report)
        self.assertTrue(report["passed"])
        self.assertEqual(report["streamsInspected"], 2)
        self.assertEqual(report["feedsChanged"], 2)
        self.assertEqual(report["protocolCounts"], {"srt": 1, "dash": 1})
        self.assertTrue(report["allEndpointsDistinct"])
        self.assertFalse(report["capacityCertified"])
        self.assertFalse(report["allEndpointMediaValidated"])
        self.assertEqual(len(report["sourceStateSha256"]), 64)
        self.assertEqual(len(report["catalogConfigSha256"]), 64)
        self.assertEqual(len(self.store.load()), 2)

    def test_marked_exact_1320_catalog_import_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            states = {}
            for protocol in ("srt", "dash"):
                manifest, digest = load_fixture_manifest(
                    ROOT / f"scale/fixtures/{protocol}-endpoints-660.json"
                )
                state_path = Path(directory, f"{protocol}.json")
                write_fixture_state(state_path, fixture_state(manifest, digest))
                states[protocol] = state_path
            scenario_path = Path(directory, "mixed-1320.json")
            with patch("builtins.print"):
                run_fixture_scenario(
                    str(ROOT / "scale/fixtures/mixed-1320.json"),
                    str(states["srt"]),
                    str(states["dash"]),
                    str(scenario_path),
                )
            configs, state, _digest = load_fixture_feed_configs(
                scenario_path, require_distinct_endpoints=True
            )

        self.assertEqual(len(configs), 1320)
        self.assertEqual(state["protocolCounts"], {"srt": 660, "dash": 660})
        self.assertEqual(
            self.store.import_feeds_if_changed(configs, reject_unlisted=True), 1320
        )
        self.assertEqual(
            self.store.import_feeds_if_changed(configs, reject_unlisted=True), 0
        )
        self.assertEqual(len(self.store.load()), 1320)

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
