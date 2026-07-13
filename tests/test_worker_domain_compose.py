import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.worker-domain.yml"


@unittest.skipUnless(shutil.which("docker"), "Docker Compose is not installed")
class WorkerDomainComposeTest(unittest.TestCase):
    def test_candidate_domain_renders_exact_worker_shape(self):
        workload = json.loads(
            (ROOT / "scale/workloads/f5-1000-candidate.json").read_text(
                encoding="utf-8"
            )
        )
        shape = workload["workerShape"]
        domain = workload["placement"]["zones"][0]
        environment = os.environ | {
            "VIDEOSIM_FAILURE_DOMAIN": domain,
            "VIDEOSIM_WORKER_IMAGE": f"videosim@sha256:{'0' * 64}",
            "VIDEOSIM_CONTROL_PLANE_URL": "https://control.example.test:9443",
            "VIDEOSIM_SRT_HOST": "feeds.example.test",
            "VIDEOSIM_WORKER_CERT_DIR": "/tmp/videosim-worker-certs",
            "VIDEOSIM_WORKER_DATA_DIR": "/tmp/videosim-worker-data",
            "VIDEOSIM_STREAM_BUDGET_SECONDS": "15",
            "VIDEOSIM_DEEP_CHECK_INTERVAL_SECONDS": "3600",
            "VIDEOSIM_WORKER_HEARTBEAT_INTERVAL_SECONDS": "5",
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

        self.assertEqual(len(services), shape["failureDomainWorkerCounts"][0])
        self.assertEqual(
            {
                service["environment"]["VIDEOSIM_WORKER_ID"]
                for service in services.values()
            },
            {
                f"{domain}-worker-{index:02d}"
                for index in range(1, shape["failureDomainWorkerCounts"][0] + 1)
            },
        )
        for service in services.values():
            command = " ".join(service["command"])
            self.assertEqual(
                service["environment"]["VIDEOSIM_WORKER_HEARTBEAT_INTERVAL_SECONDS"],
                "5",
            )
            self.assertIn("--heartbeat-interval-seconds", command)
            for option, value in (
                ("--max-streams", shape["maxStreams"]),
                ("--max-srt-streams", shape["maxSrtStreams"]),
                ("--max-dash-streams", shape["maxDashStreams"]),
                ("--max-concurrent-checks", shape["maxConcurrentChecks"]),
                (
                    "--max-concurrent-deep-checks",
                    shape["maxConcurrentDeepChecks"],
                ),
            ):
                self.assertIn(f"{option} {value}", command)
            self.assertTrue(service["read_only"])
            self.assertEqual(service["cap_drop"], ["ALL"])


if __name__ == "__main__":
    unittest.main()
