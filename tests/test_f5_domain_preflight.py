import hashlib
import json
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = runpy.run_path(str(ROOT / "scripts/f5-domain-preflight.py"))
WORKLOAD = ROOT / "scale" / "workloads" / "f5-1000-candidate.json"
IMAGE_DIGEST = "sha256:" + "1" * 64
IMAGE = f"videosim@{IMAGE_DIGEST}"
IMAGE_ID = "sha256:" + "2" * 64
BEHAVIOR_COUNTS = {"healthy": 176, "slow": 22, "dead": 11, "malformed": 11}


def write_json(path: Path, value: dict) -> str:
    raw = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def host_identity(engine_id: str, name: str) -> dict:
    return {
        "schemaVersion": "videosim.docker-host-identity/v1",
        "dockerEngineId": engine_id,
        "dockerName": name,
        "osType": "linux",
        "operatingSystem": "Example Linux",
        "kernelVersion": "6.12.0",
        "architecture": "x86_64",
        "logicalCpus": 16,
        "memoryBytes": 34359738368,
        "serverVersion": "29.0.0",
    }


def fixture_state(protocol: str, host: str, domain: int) -> dict:
    streams = []
    number = 0
    for behavior, count in BEHAVIOR_COUNTS.items():
        for _ in range(count):
            number += 1
            endpoint = (
                f"srt://{host}:{22000 + domain * 1000 + number}?mode=caller"
                if protocol == "srt"
                else f"http://{host}:18083/{domain}/{number}/manifest.mpd"
            )
            streams.append(
                {
                    "id": f"{protocol}-{domain}-{number}",
                    "protocol": protocol,
                    "source": "external",
                    "status": "running",
                    "fixtureBehavior": behavior,
                    "endpoint": endpoint,
                }
            )
    return {"schemaVersion": "videosim.fixture-state/v1", "streams": streams}


def create_artifacts(root: Path, hosts: tuple[str, str, str] = ("fixture-a", "fixture-b", "fixture-c")):
    fixture_results = []
    srt_states = []
    dash_states = []
    worker_results = []
    worker_resources = []
    fixture_checks = sorted(PREFLIGHT["FIXTURE_CHECKS"])
    worker_checks = sorted(PREFLIGHT["WORKER_CHECKS"])
    for index, host in enumerate(hosts):
        inventory = {}
        for protocol, paths in (("srt", srt_states), ("dash", dash_states)):
            path = root / f"fixture-{index}-{protocol}.json"
            state_sha256 = write_json(path, fixture_state(protocol, host, index))
            paths.append(path)
            inventory[protocol] = {
                "protocol": protocol,
                "streamCount": 220,
                "distinctEndpointCount": 220,
                "behaviorEndpointCounts": BEHAVIOR_COUNTS,
                "manifestSha256": "3" * 64,
                "stateSha256": state_sha256,
            }
        fixture_result = root / f"fixture-{index}-result.json"
        write_json(
            fixture_result,
            {
                "passed": True,
                "startedAt": f"2026-07-13T10:0{index}:00+00:00",
                "endedAt": f"2026-07-13T10:0{index}:30+00:00",
                "project": f"fixture-{index}",
                "fixtureImage": IMAGE,
                "immutableImage": True,
                "hostIdentity": host_identity(
                    f"fixture-engine-{index}", f"fixture-{index}"
                ),
                "fixtureInventory": inventory,
                "checks": fixture_checks,
                "errors": [],
            },
        )
        fixture_results.append(fixture_result)

        zone = f"candidate-zone-{chr(ord('a') + index)}"
        worker_result = root / f"worker-{index}-result.json"
        worker_ids = [
            f"{zone}-worker-{number:02d}" for number in range(1, 12)
        ]
        resource_path = root / f"worker-{index}-docker-stats.jsonl"
        resource_raw = "\n".join(
            json.dumps(
                {
                    "ID": f"{index + 1:x}{number:011x}",
                    "CPUPerc": "0.01%",
                    "MemUsage": "10MiB / 1GiB",
                    "PIDs": "4",
                },
                sort_keys=True,
            )
            for number in range(1, len(worker_ids) + 1)
        ).encode()
        resource_path.write_bytes(resource_raw)
        write_json(
            worker_result,
            {
                "passed": True,
                "startedAt": f"2026-07-13T10:1{index}:00+00:00",
                "endedAt": f"2026-07-13T10:1{index}:30+00:00",
                "project": f"worker-{index}",
                "failureDomain": zone,
                "expectedWorkerIds": worker_ids,
                "workerRegistrations": [
                    {
                        "workerId": worker_id,
                        "workerIncarnationId": (
                            "00000000-0000-0000-0000-"
                            f"{index * 11 + number:012d}"
                        ),
                    }
                    for number, worker_id in enumerate(worker_ids, start=1)
                ],
                "workerImage": IMAGE,
                "immutableImage": True,
                "hostIdentity": host_identity(
                    f"worker-engine-{index}", f"worker-{index}"
                ),
                "resolvedImageId": IMAGE_ID,
                "checks": worker_checks,
                "resourceSnapshot": {
                    "artifact": "docker-stats.jsonl",
                    "sha256": hashlib.sha256(resource_raw).hexdigest(),
                    "containerCount": len(worker_ids),
                },
                "errors": [],
            },
        )
        worker_results.append(worker_result)
        worker_resources.append(resource_path)
    return fixture_results, srt_states, dash_states, worker_results, worker_resources


class F5DomainPreflightTest(unittest.TestCase):
    def verify(self, artifacts, workload=WORKLOAD):
        return PREFLIGHT["verify_f5_domain_preflight"](workload, *artifacts)

    def test_accepts_exact_three_domain_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = create_artifacts(root)
            report = self.verify(artifacts)
            output = root / "preflight.json"
            command = [
                sys.executable,
                str(ROOT / "scripts" / "f5-domain-preflight.py"),
                "--workload",
                str(WORKLOAD),
            ]
            for option, paths in zip(
                (
                    "--fixture-result",
                    "--srt-state",
                    "--dash-state",
                    "--worker-result",
                    "--worker-resource",
                ),
                artifacts,
            ):
                for path in paths:
                    command.extend((option, str(path)))
            command.extend(("--output", str(output)))
            completed = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            cli_report = json.loads(output.read_text(encoding="utf-8"))

        self.assertTrue(report["passed"], report["errors"])
        self.assertTrue(cli_report["passed"], cli_report["errors"])
        self.assertEqual(report["fixtureDomains"], 3)
        self.assertEqual(report["workerDomains"], 3)
        self.assertEqual(report["workerCount"], 33)
        self.assertEqual(report["workerRegistrationCount"], 33)
        self.assertEqual(report["workerIncarnationCount"], 33)
        self.assertEqual(report["workerResourceSnapshotCount"], 3)
        self.assertEqual(len(report["inputSha256"]["workerResourceSnapshots"]), 3)
        self.assertEqual(report["protocolCounts"], {"srt": 660, "dash": 660})
        self.assertEqual(len(report["advertisedHosts"]), 3)
        self.assertEqual(report["dockerHostCount"], 6)
        self.assertTrue(report["distinctDockerHostsValidated"])
        self.assertFalse(report["independentHostsCertified"])
        self.assertFalse(report["capacityCertified"])

    def test_rejects_reused_advertised_host(self):
        with tempfile.TemporaryDirectory() as directory:
            report = self.verify(
                create_artifacts(Path(directory), ("fixture-a", "fixture-a", "fixture-c"))
            )

        self.assertFalse(report["passed"])
        self.assertIn("fixture domains must use distinct advertised hosts", report["errors"])

    def test_rejects_tampered_state_and_worker_image_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = create_artifacts(Path(directory))
            artifacts[1][0].write_text(
                artifacts[1][0].read_text(encoding="utf-8") + "\n", encoding="utf-8"
            )
            worker = json.loads(artifacts[3][2].read_text(encoding="utf-8"))
            worker["workerImage"] = "videosim@sha256:" + "4" * 64
            write_json(artifacts[3][2], worker)
            report = self.verify(artifacts)

        self.assertFalse(report["passed"])
        self.assertIn(
            "fixture domain 1 srt inventory does not match its state", report["errors"]
        )
        self.assertIn(
            "fixture and worker domains must use one immutable image digest",
            report["errors"],
        )

    def test_rejects_reused_docker_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = create_artifacts(Path(directory))
            worker = json.loads(artifacts[3][0].read_text(encoding="utf-8"))
            worker["hostIdentity"] = host_identity("fixture-engine-0", "worker-0")
            write_json(artifacts[3][0], worker)
            report = self.verify(artifacts)

        self.assertFalse(report["passed"])
        self.assertFalse(report["distinctDockerHostsValidated"])
        self.assertIn(
            "domain startup artifacts must come from 6 distinct Docker engines",
            report["errors"],
        )

    def test_rejects_reused_worker_incarnation(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = create_artifacts(Path(directory))
            worker = json.loads(artifacts[3][1].read_text(encoding="utf-8"))
            worker["workerRegistrations"][0]["workerIncarnationId"] = (
                "00000000-0000-0000-0000-000000000001"
            )
            write_json(artifacts[3][1], worker)
            report = self.verify(artifacts)

        self.assertFalse(report["passed"])
        self.assertIn(
            "worker domain 2 reuses worker incarnation IDs",
            report["errors"],
        )

    def test_rejects_incomplete_worker_resource_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = create_artifacts(Path(directory))
            worker = json.loads(artifacts[3][0].read_text(encoding="utf-8"))
            worker["resourceSnapshot"]["containerCount"] = 10
            write_json(artifacts[3][0], worker)
            report = self.verify(artifacts)

        self.assertFalse(report["passed"])
        self.assertIn(
            "worker domain 1.resourceSnapshot.containerCount must be 11",
            report["errors"],
        )

    def test_rejects_missing_worker_resource_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = create_artifacts(Path(directory))
            report = self.verify((*artifacts[:4], []))

        self.assertFalse(report["passed"])
        self.assertIn(
            "worker resource snapshots must contain exactly 3 files", report["errors"]
        )

    def test_rejects_tampered_and_malformed_worker_resource_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = create_artifacts(Path(directory))
            artifacts[4][0].write_bytes(artifacts[4][0].read_bytes() + b"\n")
            artifacts[4][1].write_text(
                "\n".join(
                    json.dumps(
                        {
                            "ID": f"2{number:011x}",
                            "CPUPerc": "" if number == 1 else "0.01%",
                            "MemUsage": "10MiB / 1GiB",
                            "PIDs": "4",
                        }
                    )
                    for number in range(1, 12)
                ),
                encoding="utf-8",
            )
            report = self.verify(artifacts)

        self.assertFalse(report["passed"])
        self.assertIn(
            "worker domain 1.resourceSnapshot.sha256 does not match its artifact",
            report["errors"],
        )
        self.assertIn(
            "worker domain 2 resource snapshot row 1 is missing metrics",
            report["errors"],
        )
        self.assertIn(
            "worker domain 2 resource snapshot contains 10 valid rows, expected 11",
            report["errors"],
        )

    def test_rejects_non_f5_workload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = create_artifacts(root)
            workload = json.loads(WORKLOAD.read_text(encoding="utf-8"))
            workload["targetStreams"] = 999
            workload_path = root / "workload.json"
            write_json(workload_path, workload)
            report = self.verify(artifacts, workload_path)

        self.assertFalse(report["passed"])
        self.assertIn("workload.targetStreams must be 1000", report["errors"])


if __name__ == "__main__":
    unittest.main()
