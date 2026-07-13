import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_COMPOSE = ROOT / "docker-compose.production.yml"
RUNTIME_COMPOSE = ROOT / "docker-compose.generated-runtime.yml"


@unittest.skipUnless(shutil.which("docker"), "Docker Compose is not installed")
class GeneratedRuntimeComposeTest(unittest.TestCase):
    def test_runtime_overlay_renders_separate_media_services(self):
        environment = os.environ | {
            "POSTGRES_PASSWORD": "owner-password",
            "POSTGRES_APP_PASSWORD": "app-password",
            "POSTGRES_PUBLISHER_PASSWORD": "publisher-password",
            "POSTGRES_PRUNER_PASSWORD": "pruner-password",
            "NATS_PASSWORD": "nats-password",
            "VIDEOSIM_PROXY_SHARED_SECRET": "x" * 32,
            "OIDC_ISSUER_URL": "https://identity.example.test",
            "OIDC_CLIENT_ID": "videosim",
            "OIDC_CLIENT_SECRET": "oidc-secret",
            "OIDC_COOKIE_SECRET": "cookie-secret",
        }
        result = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(PRODUCTION_COMPOSE),
                "-f",
                str(RUNTIME_COMPOSE),
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
        runtime = services["generated-feed-runtime"]
        origin = services["generated-dash-origin"]

        self.assertIn("videosim.generated_runtime", " ".join(runtime["command"]))
        self.assertEqual(runtime["init"], True)
        self.assertEqual(runtime["restart"], "unless-stopped")
        self.assertEqual(origin["ports"][0]["target"], 8090)
        self.assertEqual(
            services["app"]["environment"]["VIDEOSIM_GENERATED_SRT_HOST"],
            "generated-feed-runtime",
        )
        self.assertEqual(
            services["app"]["environment"]["VIDEOSIM_GENERATED_DASH_BASE_URL"],
            "http://generated-dash-origin:8090",
        )


if __name__ == "__main__":
    unittest.main()
