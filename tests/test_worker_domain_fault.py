import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/worker-domain-fault.py"
FAULT = runpy.run_path(str(SCRIPT))
WORKLOAD = json.loads(
    (ROOT / "scale/workloads/f5-1000-candidate.json").read_text(encoding="utf-8")
)
INTEGRATION = os.environ.get("VIDEOSIM_WORKER_DOMAIN_FAULT_INTEGRATION") == "1"


def startup_result(domain="candidate-zone-a"):
    worker_ids = [f"{domain}-worker-{number:02d}" for number in range(1, 12)]
    return {
        "passed": True,
        "errors": [],
        "checks": [
            "docker_resources_captured",
            "worker_registrations_validated",
            "worker_services_stable",
            "docker_logs_clean",
        ],
        "project": "worker-domain-a",
        "failureDomain": domain,
        "expectedWorkerIds": worker_ids,
        "workerRegistrations": [
            {
                "workerId": worker_id,
                "workerIncarnationId": (
                    "00000000-0000-0000-0000-" f"{number:012d}"
                ),
            }
            for number, worker_id in enumerate(worker_ids, start=1)
        ],
        "resourceSnapshot": {
            "artifact": "docker-stats.jsonl",
            "sha256": "a" * 64,
            "containerCount": len(worker_ids),
        },
    }


class WorkerDomainFaultTest(unittest.TestCase):
    def test_startup_result_requires_exact_unique_registrations(self):
        project, domain, worker_ids = FAULT["validate_startup_result"](
            startup_result(), WORKLOAD
        )

        self.assertEqual(project, "worker-domain-a")
        self.assertEqual(domain, "candidate-zone-a")
        self.assertEqual(len(worker_ids), 11)
        duplicate = startup_result()
        duplicate["workerRegistrations"][1]["workerIncarnationId"] = (
            duplicate["workerRegistrations"][0]["workerIncarnationId"]
        )
        with self.assertRaisesRegex(ValueError, "canonical and unique"):
            FAULT["validate_startup_result"](duplicate, WORKLOAD)
        incomplete_snapshot = startup_result()
        incomplete_snapshot["resourceSnapshot"]["containerCount"] = 10
        with self.assertRaisesRegex(ValueError, "does not cover"):
            FAULT["validate_startup_result"](incomplete_snapshot, WORKLOAD)

    def test_authority_and_recovery_reject_healthy_owner_churn(self):
        snapshot = self.authority_snapshot()
        owners = FAULT["authoritative_owners"](snapshot)
        recovery_times = {}

        FAULT["update_recovery_times"](
            owners,
            {
                "affected": "candidate-zone-c-worker-01",
                "healthy": owners["healthy"],
            },
            {"affected"},
            recovery_times,
            31.25,
        )

        self.assertEqual(recovery_times, {"affected": 31.25})
        with self.assertRaisesRegex(RuntimeError, "healthy-domain authority changed"):
            FAULT["update_recovery_times"](
                owners,
                {
                    "affected": "candidate-zone-c-worker-01",
                    "healthy": "candidate-zone-c-worker-02",
                },
                {"affected"},
                recovery_times,
                32,
            )

    def test_recovery_percentiles_are_ordered(self):
        metrics = FAULT["recovery_percentiles"]([1, 2, 3, 4, 5])

        self.assertEqual(metrics["p50"], 3)
        self.assertLessEqual(metrics["p50"], metrics["p95"])
        self.assertLessEqual(metrics["p95"], metrics["p99"])
        self.assertEqual(metrics["maximum"], 5)

    def test_survivor_incarnation_must_not_change(self):
        baseline = self.authority_snapshot()
        current = json.loads(json.dumps(baseline))
        current["workers"][1]["incarnationId"] = "incarnation-b-new"
        current["leases"][1]["workerIncarnationId"] = "incarnation-b-new"

        with self.assertRaisesRegex(RuntimeError, "survivor worker incarnation"):
            FAULT["require_survivors_unchanged"](
                baseline,
                current,
                "candidate-zone-a",
            )

    def test_assignment_baseline_retries_transient_failure(self):
        workflow = object.__new__(FAULT["WorkerDomainFault"])
        workflow.baseline_timeout = 0.1
        workflow.poll_seconds = 0.001
        attempts = iter(
            [
                (
                    {"snapshot": 1},
                    SimpleNamespace(
                        passed=False,
                        metrics={"phase": "baseline"},
                        errors=["one worker heartbeat is temporarily stale"],
                    ),
                ),
                (
                    {"snapshot": 2},
                    SimpleNamespace(
                        passed=True,
                        metrics={"phase": "baseline"},
                        errors=[],
                    ),
                ),
            ]
        )
        workflow.snapshot_report = lambda: next(attempts)

        snapshot, report = workflow.wait_for_baseline()

        self.assertEqual(snapshot, {"snapshot": 2})
        self.assertTrue(report.passed)

    @staticmethod
    def authority_snapshot():
        return {
            "capturedAt": "2026-07-13T10:00:00+00:00",
            "feeds": [
                {"streamId": "affected", "configVersion": 1, "desired": True},
                {"streamId": "healthy", "configVersion": 1, "desired": True},
            ],
            "workers": [
                {
                    "workerId": "candidate-zone-a-worker-01",
                    "incarnationId": "incarnation-a",
                    "state": "active",
                    "heartbeatFresh": True,
                },
                {
                    "workerId": "candidate-zone-b-worker-01",
                    "incarnationId": "incarnation-b",
                    "state": "active",
                    "heartbeatFresh": True,
                },
            ],
            "leases": [
                {
                    "streamId": "affected",
                    "workerId": "candidate-zone-a-worker-01",
                    "workerIncarnationId": "incarnation-a",
                    "configVersion": 1,
                    "state": "active",
                    "expiresAt": "2026-07-13T10:01:00+00:00",
                },
                {
                    "streamId": "healthy",
                    "workerId": "candidate-zone-b-worker-01",
                    "workerIncarnationId": "incarnation-b",
                    "configVersion": 1,
                    "state": "active",
                    "expiresAt": "2026-07-13T10:01:00+00:00",
                },
            ],
        }


@unittest.skipUnless(
    INTEGRATION,
    "set VIDEOSIM_WORKER_DOMAIN_FAULT_INTEGRATION=1 on a complete F5 coordinator",
)
class WorkerDomainFaultIntegrationTest(unittest.TestCase):
    def test_hard_stops_recovers_and_rejoins_one_worker_domain(self):
        startup = os.environ["VIDEOSIM_WORKER_FAULT_STARTUP_RESULT"]
        database_url = os.environ["VIDEOSIM_TEST_POSTGRES_URL"]
        retained = os.environ.get("VIDEOSIM_WORKER_FAULT_ARTIFACT_DIR")
        context = nullcontext(retained) if retained else tempfile.TemporaryDirectory()
        with context as directory:
            Path(directory).mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--startup-result",
                    startup,
                    "--database-url",
                    database_url,
                    "--artifact-dir",
                    directory,
                ],
                cwd=ROOT,
                env=os.environ.copy(),
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
        self.assertEqual(evidence["recovery"]["affectedStreams"], 440)
        self.assertIn("domain_loss_authority_recovered", evidence["checks"])
        self.assertIn("worker_domain_rejoined", evidence["checks"])
        self.assertIn("docker_logs_clean", evidence["checks"])


if __name__ == "__main__":
    unittest.main()
