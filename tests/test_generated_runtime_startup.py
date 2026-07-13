import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = os.environ.get("VIDEOSIM_GENERATED_RUNTIME_INTEGRATION") == "1"


@unittest.skipUnless(
    INTEGRATION,
    "set VIDEOSIM_GENERATED_RUNTIME_INTEGRATION=1 to run generated runtime startup validation",
)
class GeneratedRuntimeStartupIntegrationTest(unittest.TestCase):
    def test_two_api_runtime_restart_and_media_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ | {
                "VIDEOSIM_GENERATED_VALIDATION_ARTIFACT_DIR": directory
            }
            result = subprocess.run(
                [sys.executable, "scripts/generated-runtime-validation.py"],
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
            self.assertEqual(
                evidence["checks"],
                [
                    "two_apis_ready",
                    "replica_create_visible",
                    "remote_start_reconciled",
                    "media_validated_before_restart",
                    "media_validated_after_restart",
                    "remote_stop_unreachable",
                ],
            )
            logs = Path(directory, "docker.log").read_text(encoding="utf-8")
            self.assertNotIn("Traceback (most recent call last)", logs)
            self.assertNotIn("fatal=", logs)


if __name__ == "__main__":
    unittest.main()
