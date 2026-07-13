import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STARTUP = runpy.run_path(str(ROOT / "scripts/durable-fixture-startup.py"))
STARTUP_INTEGRATION = (
    os.environ.get("VIDEOSIM_DURABLE_FIXTURE_STARTUP_INTEGRATION") == "1"
)


class DurableFixtureSummaryTest(unittest.TestCase):
    def test_requires_exact_worker_media_and_alarm_state(self):
        expected_counts = {
            f"{protocol}:{behavior}": 1
            for protocol in ("srt", "dash")
            for behavior in ("healthy", "slow", "dead", "malformed")
        }
        summary = {
            "freshWorkers": 2,
            "activeLeases": 8,
            "minimumLeases": 4,
            "maximumLeases": 4,
            "validatedStreams": 8,
            "blackFrozenAlarms": 0,
            "activeAlarmCount": 4,
            "outcomes": {
                "dash:dead:timeout": 1,
                "dash:healthy:healthy": 1,
                "dash:malformed:unhealthy": 1,
                "dash:slow:timeout": 1,
                "srt:dead:timeout": 1,
                "srt:healthy:healthy": 1,
                "srt:malformed:timeout": 1,
                "srt:slow:timeout": 1,
            },
            "activeAlarms": {
                f"dash:malformed:{monitor_id}": 1
                for monitor_id in STARTUP["VALIDATION_MONITORS"]
            },
        }

        validate = STARTUP["validate_durable_summary"]
        self.assertEqual(validate(summary, expected_counts, 2), [])

        summary["outcomes"]["srt:healthy:timeout"] = 1
        summary["outcomes"]["srt:healthy:healthy"] = 0
        summary["blackFrozenAlarms"] = 1
        errors = validate(summary, expected_counts, 2)

        self.assertTrue(any(error.startswith("outcomes=") for error in errors))
        self.assertIn("blackFrozenAlarms=1 expected 0", errors)


@unittest.skipUnless(
    STARTUP_INTEGRATION,
    "set VIDEOSIM_DURABLE_FIXTURE_STARTUP_INTEGRATION=1 to run the durable fixture startup",
)
class DurableFixtureStartupIntegrationTest(unittest.TestCase):
    def test_boots_durable_workers_and_validates_api_media_alarms_and_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scenario_manifest = root / "mixed-8.json"
            scenario_manifest.write_text(
                json.dumps(
                    {
                        "behaviorPercent": {
                            "healthy": 25,
                            "slow": 25,
                            "dead": 25,
                            "malformed": 25,
                        },
                        "protocolPercent": {"srt": 50, "dash": 50},
                        "randomSeed": 17,
                        "schemaVersion": "videosim.fixture-scenario/v1",
                        "streamCount": 8,
                    }
                ),
                encoding="utf-8",
            )
            artifact_dir = root / "artifacts"
            environment = os.environ | {
                "VIDEOSIM_DURABLE_FIXTURE_PROJECT": "videosim-durable-fixture-test",
                "VIDEOSIM_DURABLE_FIXTURE_ARTIFACT_DIR": str(artifact_dir),
                "VIDEOSIM_DURABLE_FIXTURE_SCENARIO_MANIFEST": str(
                    scenario_manifest
                ),
                "VIDEOSIM_DURABLE_FIXTURE_WORKER_COUNT": "2",
                "VIDEOSIM_DURABLE_FIXTURE_TIMEOUT_SECONDS": "180",
                "VIDEOSIM_DURABLE_FIXTURE_POSTGRES_PORT": "55441",
                "VIDEOSIM_DURABLE_FIXTURE_HTTP_PORT": "18086",
                "VIDEOSIM_DURABLE_FIXTURE_FEED_PORT": "29970",
                "VIDEOSIM_DURABLE_FIXTURE_ALLOW_MUTABLE_IMAGE": "1",
                "VIDEOSIM_DURABLE_FIXTURE_NO_PULL": "1",
                "VIDEOSIM_SRT_FIXTURE_MANIFEST": "scale/fixtures/srt-matrix.json",
                "VIDEOSIM_DASH_FIXTURE_MANIFEST": "scale/fixtures/dash-matrix.json",
                "VIDEOSIM_DASH_FIXTURE_HTTP_PORT": "18081",
            }
            result = subprocess.run(
                [sys.executable, "scripts/durable-fixture-startup.py"],
                cwd=ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=600,
            )
            evidence = json.loads(
                (artifact_dir / "result.json").read_text(encoding="utf-8")
            )
            container_state = json.loads(
                (artifact_dir / "container-state.json").read_text(encoding="utf-8")
            )
            stats = [
                json.loads(line)
                for line in (artifact_dir / "docker-stats.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(evidence["passed"])
        self.assertTrue(evidence["allPathMediaValidated"])
        self.assertFalse(evidence["capacityCertified"])
        self.assertEqual(evidence["validationSurface"], "api")
        self.assertIn("durable_media_alarms_validated", evidence["checks"])
        self.assertIn("operator_api_catalog_validated", evidence["checks"])
        self.assertIn("docker_state_stats_logs_captured", evidence["checks"])
        self.assertEqual(evidence["apiCatalog"]["feedCount"], 8)
        fixture_services = {
            item["Config"]["Labels"].get("com.docker.compose.service")
            for item in container_state
        }
        self.assertTrue({"srt", "dash"}.issubset(fixture_services))
        self.assertEqual(len(stats), 6)


if __name__ == "__main__":
    unittest.main()
