import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/worker-domain-startup.py"
STARTUP = runpy.run_path(str(SCRIPT))
STARTUP_INTEGRATION = (
    os.environ.get("VIDEOSIM_WORKER_DOMAIN_STARTUP_INTEGRATION") == "1"
)


class WorkerDomainStartupTest(unittest.TestCase):
    def test_candidate_domain_has_exact_eleven_worker_identities(self):
        worker_ids = STARTUP["candidate_worker_ids"]("candidate-zone-b")

        self.assertEqual(len(worker_ids), 11)
        self.assertEqual(worker_ids[0], "candidate-zone-b-worker-01")
        self.assertEqual(worker_ids[-1], "candidate-zone-b-worker-11")
        with self.assertRaisesRegex(ValueError, "must be one of"):
            STARTUP["candidate_worker_ids"]("unknown-zone")

    @unittest.skipUnless(shutil.which("openssl"), "openssl is not installed")
    def test_existing_generator_produces_valid_worker_inventory(self):
        worker_id = "candidate-zone-a-worker-01"
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(
                [
                    "bash",
                    "scripts/generate-dev-mtls-certs.sh",
                    directory,
                    worker_id,
                ],
                cwd=ROOT,
                check=True,
                stdout=subprocess.DEVNULL,
            )
            inventory = STARTUP["validate_certificate_inventory"](
                Path(directory), (worker_id,)
            )

        self.assertEqual(len(inventory), 1)
        self.assertEqual(inventory[0]["workerId"], worker_id)
        self.assertEqual(inventory[0]["keyMode"], "0600")
        self.assertEqual(inventory[0]["spoolKeyMode"], "0600")
        self.assertEqual(len(inventory[0]["certificateSha256"]), 64)

    def test_container_validation_rejects_restart_and_image_drift(self):
        services = {f"worker-{number:02d}" for number in range(1, 12)}
        records = [
            {
                "service": service,
                "imageId": "sha256:expected",
                "running": True,
                "restartCount": 0,
            }
            for service in sorted(services)
        ]
        validate = STARTUP["validate_container_records"]

        validate(records, services, "sha256:expected")
        records[0]["restartCount"] = 1
        with self.assertRaisesRegex(RuntimeError, "restarted 1 time"):
            validate(records, services, "sha256:expected")
        records[0]["restartCount"] = 0
        records[0]["imageId"] = "sha256:other"
        with self.assertRaisesRegex(RuntimeError, "resolved worker image"):
            validate(records, services, "sha256:expected")

    def test_critical_worker_log_markers_are_fail_closed(self):
        matches = STARTUP["critical_log_matches"](
            "worker ready\nTraceback (most recent call last):\n"
            "[videosim-worker] report_spool=blocked queued_reports=1"
        )

        self.assertEqual(
            matches,
            ["traceback (most recent call last)", "report_spool=blocked"],
        )
        self.assertEqual(STARTUP["critical_log_matches"]("worker ready\n"), [])


@unittest.skipUnless(
    STARTUP_INTEGRATION,
    "set VIDEOSIM_WORKER_DOMAIN_STARTUP_INTEGRATION=1 on a configured worker host",
)
class WorkerDomainStartupIntegrationTest(unittest.TestCase):
    def test_boots_exact_worker_domain_and_captures_api_process_and_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ | {
                "VIDEOSIM_WORKER_STARTUP_ARTIFACT_DIR": directory,
            }
            result = subprocess.run(
                [sys.executable, str(SCRIPT)],
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
            docker_log = Path(directory, "docker.log").read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(evidence["passed"])
        self.assertEqual(len(evidence["expectedWorkerIds"]), 11)
        self.assertIn("control_plane_health_api", evidence["checks"])
        self.assertIn("worker_mtls_health_api", evidence["checks"])
        self.assertIn("worker_services_stable", evidence["checks"])
        self.assertIn("docker_logs_clean", evidence["checks"])
        self.assertTrue(docker_log.strip())


if __name__ == "__main__":
    unittest.main()
