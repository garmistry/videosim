import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from videosim.fixture_fleet import fixture_state, load_fixture_manifest


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.fixture-domain.yml"
STARTUP = runpy.run_path(str(ROOT / "scripts/fixture-domain-startup.py"))
FAULT = runpy.run_path(str(ROOT / "scripts/fixture-domain-fault.py"))
STARTUP_INTEGRATION = (
    os.environ.get("VIDEOSIM_FIXTURE_STARTUP_INTEGRATION") == "1"
)


class FixtureDomainStartupTest(unittest.TestCase):
    def test_fault_outcomes_require_one_result_in_the_expected_state(self):
        validate = FAULT["validate_media_outcomes"]

        healthy = {
            "results": {
                "validationOutcomesByProtocol": {
                    "srt": {"success": 1},
                    "dash": {"success": 1},
                }
            }
        }
        faulted = {
            "results": {
                "validationOutcomesByProtocol": {
                    "srt": {"timeout": 1},
                    "dash": {"issue": 1},
                }
            }
        }

        self.assertEqual(
            validate(healthy, healthy=True),
            healthy["results"]["validationOutcomesByProtocol"],
        )
        self.assertEqual(
            validate(faulted, healthy=False),
            faulted["results"]["validationOutcomesByProtocol"],
        )
        with self.assertRaisesRegex(RuntimeError, "was not unreachable"):
            validate(healthy, healthy=False)

    def test_inventory_requires_every_declared_endpoint_to_be_distinct(self):
        manifest_path = ROOT / "scale/fixtures/srt-endpoints-220-domain.json"
        manifest, digest = load_fixture_manifest(manifest_path)
        state = fixture_state(manifest, digest)
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "srt-state.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            inventory = STARTUP["validate_fixture_inventory"](
                state_path, manifest_path, "srt"
            )

            self.assertEqual(inventory["streamCount"], 220)
            self.assertEqual(inventory["distinctEndpointCount"], 220)
            self.assertEqual(
                inventory["behaviorEndpointCounts"],
                {"healthy": 176, "slow": 22, "dead": 11, "malformed": 11},
            )

            state["streams"][1]["endpoint"] = state["streams"][0]["endpoint"]
            state_path.write_text(json.dumps(state), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "duplicate endpoints"):
                STARTUP["validate_fixture_inventory"](
                    state_path, manifest_path, "srt"
                )


@unittest.skipUnless(shutil.which("docker"), "Docker Compose is not installed")
class FixtureDomainComposeTest(unittest.TestCase):
    def test_candidate_fixture_domain_renders_linux_host_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ | {
                "VIDEOSIM_FIXTURE_IMAGE": f"videosim@sha256:{'0' * 64}",
                "VIDEOSIM_FIXTURE_ADVERTISED_HOST": "fixture-a.example.test",
                "VIDEOSIM_FIXTURE_STATE_DIR": directory,
            }
            result = subprocess.run(
                [
                    "docker",
                    "compose",
                    "-f",
                    str(COMPOSE),
                    "config",
                    "--format",
                    "json",
                ],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=True,
            )
        services = json.loads(result.stdout)["services"]

        self.assertEqual(set(services), {"srt", "dash"})
        for service in services.values():
            self.assertEqual(service["network_mode"], "host")
            self.assertTrue(service["read_only"])
            self.assertEqual(service["cap_drop"], ["ALL"])
            self.assertEqual(service["pids_limit"], 4096)
            self.assertEqual(
                service["environment"]["VIDEOSIM_FIXTURE_ADVERTISED_HOST"],
                "fixture-a.example.test",
            )
            self.assertEqual(service["volumes"][0]["target"], "/artifacts")

        self.assertIn(
            "srt-endpoints-220-domain.json",
            services["srt"]["environment"]["VIDEOSIM_FIXTURE_MANIFEST"],
        )
        self.assertIn(
            "dash-endpoints-220-domain.json",
            services["dash"]["environment"]["VIDEOSIM_FIXTURE_MANIFEST"],
        )
        self.assertIn("VIDEOSIM_FIXTURE_MANIFEST", services["srt"]["command"][0])
        self.assertIn("VIDEOSIM_FIXTURE_MANIFEST", services["dash"]["command"][0])
        self.assertEqual(
            services["dash"]["environment"]["VIDEOSIM_DASH_FIXTURE_HTTP_PORT"],
            "18083",
        )


@unittest.skipUnless(
    STARTUP_INTEGRATION,
    "set VIDEOSIM_FIXTURE_STARTUP_INTEGRATION=1 to run fixture startup validation",
)
class FixtureDomainStartupIntegrationTest(unittest.TestCase):
    def test_boots_faults_recovers_and_validates_srt_dash_media_and_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            artifact_dir = Path(directory) / "artifacts"
            fault_dir = Path(directory) / "fault"
            environment = os.environ | {
                "VIDEOSIM_FIXTURE_IMAGE": os.environ.get(
                    "VIDEOSIM_FIXTURE_IMAGE",
                    "videosim-startup-validation-app:latest",
                ),
                "VIDEOSIM_FIXTURE_ADVERTISED_HOST": "127.0.0.1",
                "VIDEOSIM_FIXTURE_STATE_DIR": str(state_dir),
                "VIDEOSIM_FIXTURE_STARTUP_ARTIFACT_DIR": str(artifact_dir),
                "VIDEOSIM_FIXTURE_FAULT_ARTIFACT_DIR": str(fault_dir),
                "VIDEOSIM_FIXTURE_FAULT_HOLD_SECONDS": "0",
                "VIDEOSIM_FIXTURE_STARTUP_NO_PULL": "1",
                "VIDEOSIM_FIXTURE_STARTUP_ALLOW_MUTABLE_IMAGE": "1",
                "VIDEOSIM_FIXTURE_STARTUP_KEEP": "1",
                "VIDEOSIM_SRT_FIXTURE_MANIFEST": "scale/fixtures/srt-matrix.json",
                "VIDEOSIM_DASH_FIXTURE_MANIFEST": "scale/fixtures/dash-matrix.json",
                "VIDEOSIM_DASH_FIXTURE_HTTP_PORT": "18081",
            }
            try:
                result = subprocess.run(
                    [sys.executable, "scripts/fixture-domain-startup.py"],
                    cwd=ROOT,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=300,
                )
                fault_result = subprocess.run(
                    [sys.executable, "scripts/fixture-domain-fault.py"],
                    cwd=ROOT,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=300,
                )
            finally:
                subprocess.run(
                    [
                        "docker",
                        "compose",
                        "-p",
                        environment.get(
                            "VIDEOSIM_FIXTURE_STARTUP_PROJECT",
                            "videosim-fixture-startup",
                        ),
                        "-f",
                        str(COMPOSE),
                        "down",
                        "--remove-orphans",
                    ],
                    cwd=ROOT,
                    env=environment,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=180,
                )
            evidence = json.loads(
                (artifact_dir / "result.json").read_text(encoding="utf-8")
            )
            fault_evidence = json.loads(
                (fault_dir / "result.json").read_text(encoding="utf-8")
            )
            docker_log = (artifact_dir / "docker.log").read_text(encoding="utf-8")
            docker_stats = (artifact_dir / "docker-stats.json").read_text(
                encoding="utf-8"
            )
            compose_ps = (artifact_dir / "compose-ps.txt").read_text(
                encoding="utf-8"
            )

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(fault_result.returncode, 0, fault_result.stdout)
        self.assertTrue(evidence["passed"])
        self.assertIn("dash_health_api", evidence["checks"])
        self.assertIn("fixture_inventory_validated", evidence["checks"])
        self.assertIn("srt_dash_media_validated", evidence["checks"])
        self.assertIn("docker_resources_captured", evidence["checks"])
        self.assertIn("docker_logs_clean", evidence["checks"])
        self.assertTrue(docker_log.strip())
        self.assertTrue(docker_stats.strip())
        self.assertNotIn("Traceback (most recent call last)", docker_log)
        self.assertIn("srt", compose_ps)
        self.assertIn("dash", compose_ps)
        self.assertEqual(
            evidence["validationOutcomesByProtocol"],
            {"dash": {"success": 1}, "srt": {"success": 1}},
        )
        for inventory in evidence["fixtureInventory"].values():
            self.assertEqual(inventory["streamCount"], 4)
            self.assertEqual(inventory["distinctEndpointCount"], 4)
        self.assertTrue(fault_evidence["passed"])
        self.assertTrue(fault_evidence["servicesRunningAtEnd"])
        self.assertFalse(fault_evidence["capacityCertified"])
        self.assertFalse(fault_evidence["allEndpointMediaValidated"])
        self.assertEqual(
            fault_evidence["baselineOutcomesByProtocol"],
            {"dash": {"success": 1}, "srt": {"success": 1}},
        )
        self.assertNotIn(
            "success", fault_evidence["faultOutcomesByProtocol"]["dash"]
        )
        self.assertNotIn(
            "success", fault_evidence["faultOutcomesByProtocol"]["srt"]
        )
        self.assertEqual(
            fault_evidence["recoveryOutcomesByProtocol"],
            {"dash": {"success": 1}, "srt": {"success": 1}},
        )


if __name__ == "__main__":
    unittest.main()
