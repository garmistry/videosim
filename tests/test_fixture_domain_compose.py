import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.fixture-domain.yml"


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


if __name__ == "__main__":
    unittest.main()
