import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STARTUP_INTEGRATION = os.environ.get("VIDEOSIM_STARTUP_INTEGRATION") == "1"


@unittest.skipUnless(
    STARTUP_INTEGRATION,
    "set VIDEOSIM_STARTUP_INTEGRATION=1 to run Docker startup validation",
)
class StartupWorkflowIntegrationTest(unittest.TestCase):
    def test_compose_startup_and_api_media_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment["VIDEOSIM_STARTUP_ARTIFACT_DIR"] = directory
            result = subprocess.run(
                [sys.executable, "scripts/startup-validation.py"],
                cwd=ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=900,
            )
            evidence = json.loads(
                Path(directory, "result.json").read_text(encoding="utf-8")
            )

            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertTrue(evidence["passed"])
            self.assertIn("normal_feed_validated", evidence["checks"])
            docker_log = Path(directory, "docker.log")
            self.assertTrue(docker_log.is_file())
            log_output = docker_log.read_text(encoding="utf-8")
            self.assertTrue(log_output.strip())
            self.assertNotIn("Traceback (most recent call last)", log_output)
            self.assertTrue(Path(directory, "compose-ps.txt").is_file())
