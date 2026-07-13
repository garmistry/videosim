import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from videosim.cli import main
from videosim.scale_evidence import (
    ADMISSION_CRITERIA,
    BASELINE_ARTIFACT_KINDS,
    check_scale_evidence,
)


class ScaleEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.policy = {
            "schemaVersion": "videosim.capacity-policy/v1",
            "policyVersion": "f5-test-v1",
            "targetStreams": 1000,
            "minimumDurationSeconds": 86400,
            "minimumHeadroomPercent": 30,
            "minimumUnavailableFailureDomains": 1,
            "requireCleanSource": True,
            "allowSkippedChecks": False,
            "requiredCriteria": list(ADMISSION_CRITERIA),
            "requiredArtifactKinds": list(BASELINE_ARTIFACT_KINDS),
        }
        self.workload = {
            "schemaVersion": "videosim.scale-workload/v1",
            "randomSeed": 17,
            "targetStreams": 1000,
            "loadStreams": 1300,
            "headroomPercent": 30,
            "failureDomainsUnavailable": 1,
            "protocolMix": {"srtPercent": 80, "dashPercent": 20},
            "sourceMix": {"generatedPercent": 20, "externalPercent": 80},
            "placement": {
                "regions": ["us-east"],
                "zones": ["us-east-a", "us-east-b", "us-east-c"],
            },
            "checkProfiles": [{"name": "critical", "cadenceSeconds": 5}],
            "endpointDistribution": {
                "healthyPercent": 80,
                "slowPercent": 10,
                "deadPercent": 5,
                "malformedPercent": 5,
                "latencyMs": {"p50": 10, "p95": 100, "p99": 500},
            },
            "eventStorms": [{"offsetSeconds": 3600, "kind": "worker-loss"}],
            "workerShape": {
                "count": 15,
                "failureDomains": 3,
                "failureDomainWorkerCounts": [5, 5, 5],
                "maxStreams": 130,
                "maxSrtStreams": 104,
                "maxDashStreams": 26,
                "maxConcurrentChecks": 8,
                "maxConcurrentDeepChecks": 2,
            },
            "infrastructureVersions": {
                "videosim": "test",
                "postgres": "17",
                "nats": "2.11",
            },
            "durationSeconds": 86400,
            "expectedInvariants": [
                "one authoritative lease per stream",
                "zero false alarm clears",
            ],
        }
        inputs = {
            "infrastructure": self.write_json("inputs/infrastructure.json", {"kind": "test"}),
            "config": self.write_json("inputs/config.json", {"workerPollSeconds": 5}),
            "workload": self.write_json("inputs/workload.json", self.workload),
        }
        artifacts = [
            {
                "kind": kind,
                **self.write_bytes(f"artifacts/{kind}.json", f"{kind}\n".encode()),
            }
            for kind in BASELINE_ARTIFACT_KINDS
        ]
        self.evidence = {
            "schemaVersion": "videosim.scale-evidence/v1",
            "sourceCommit": "a" * 40,
            "dirtySource": False,
            "containerImages": [
                {"name": "videosim", "digest": f"sha256:{'b' * 64}"}
            ],
            "inputs": inputs,
            "startedAt": "2026-07-10T00:00:00Z",
            "endedAt": "2026-07-11T00:00:00Z",
            "command": ["python", "-m", "videosim", "control-plane-load"],
            "exitStatus": 0,
            "artifacts": artifacts,
            "metrics": {
                "freshnessMs": {"p50": 100, "p95": 500, "p99": 900},
                "reassignmentMs": {"p50": 1000, "p95": 30000, "p99": 60000},
            },
            "acceptancePolicyVersion": self.policy["policyVersion"],
            "criteria": [
                {"id": criterion, "passed": True, "detail": "verified in test"}
                for criterion in ADMISSION_CRITERIA
            ],
            "skippedChecks": [],
            "approvingOwner": "release-owner@example.test",
        }
        self.policy_path = self.root / "policy.json"
        self.report_path = self.root / "evidence.json"
        self.save_manifests()

    def tearDown(self):
        self.directory.cleanup()

    def write_bytes(self, relative: str, content: bytes) -> dict:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return {
            "path": relative,
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    def write_json(self, relative: str, payload: dict) -> dict:
        return self.write_bytes(
            relative,
            (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"),
        )

    def save_manifests(self):
        self.policy_path.write_text(
            json.dumps(self.policy, sort_keys=True), encoding="utf-8"
        )
        self.report_path.write_text(
            json.dumps(self.evidence, sort_keys=True), encoding="utf-8"
        )

    def rewrite_workload(self):
        self.evidence["inputs"]["workload"] = self.write_json(
            "inputs/workload.json", self.workload
        )
        self.save_manifests()

    def test_complete_hashed_bundle_passes_and_cli_reports_json(self):
        check = check_scale_evidence(self.report_path, self.policy_path)

        self.assertTrue(check.passed, check.errors)
        self.assertEqual((check.target_streams, check.load_streams), (1000, 1300))
        self.assertEqual(check.verified_artifacts, 12)
        with patch("builtins.print") as output:
            code = main(
                [
                    "capacity-check",
                    "--report",
                    str(self.report_path),
                    "--policy",
                    str(self.policy_path),
                    "--json",
                ]
            )
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output.call_args.args[0])["passed"])

    def test_tampered_artifact_fails_closed(self):
        artifact = self.root / self.evidence["artifacts"][0]["path"]
        artifact.write_text("tampered\n", encoding="utf-8")

        check = check_scale_evidence(self.report_path, self.policy_path)

        self.assertFalse(check.passed)
        self.assertTrue(any("sha256 does not match" in error for error in check.errors))
        self.assertEqual(check.verified_artifacts, 11)

    def test_path_escape_and_missing_artifact_kind_fail_closed(self):
        outside = self.root.parent / "outside-scale-evidence.txt"
        outside.write_text("outside", encoding="utf-8")
        self.addCleanup(outside.unlink, missing_ok=True)
        self.evidence["inputs"]["infrastructure"] = {
            "path": "../outside-scale-evidence.txt",
            "sha256": hashlib.sha256(b"outside").hexdigest(),
        }
        self.evidence["artifacts"].pop()
        self.save_manifests()

        check = check_scale_evidence(self.report_path, self.policy_path)

        self.assertFalse(check.passed)
        self.assertTrue(any("escapes the evidence directory" in error for error in check.errors))
        self.assertTrue(any("omits required kinds" in error for error in check.errors))

    def test_dirty_short_failed_or_skipped_run_fails_closed(self):
        self.evidence["dirtySource"] = True
        self.evidence["endedAt"] = "2026-07-10T01:00:00Z"
        self.evidence["exitStatus"] = 1
        self.evidence["criteria"][0]["passed"] = False
        self.evidence["skippedChecks"] = ["database failover"]
        self.save_manifests()

        check = check_scale_evidence(self.report_path, self.policy_path)

        combined = "\n".join(check.errors)
        self.assertIn("dirtySource must be false", combined)
        self.assertIn("timestamp duration is below", combined)
        self.assertIn("exitStatus must be zero", combined)
        self.assertIn("did not pass", combined)
        self.assertIn("skippedChecks must be empty", combined)

    def test_workload_headroom_mix_and_failure_domains_are_enforced(self):
        self.workload["loadStreams"] = 1200
        self.workload["headroomPercent"] = 20
        self.workload["protocolMix"] = {"srtPercent": 90, "dashPercent": 20}
        self.workload["workerShape"]["failureDomains"] = 1
        self.workload["workerShape"]["maxStreams"] = 100
        self.workload["workerShape"]["maxSrtStreams"] = 80
        self.rewrite_workload()

        check = check_scale_evidence(self.report_path, self.policy_path)

        combined = "\n".join(check.errors)
        self.assertIn("loadStreams must be at least 1300", combined)
        self.assertIn("headroomPercent is below", combined)
        self.assertIn("protocolMix percentages must total 100", combined)
        self.assertIn("failureDomains must exceed", combined)
        self.assertIn("total survivor tokens cannot carry", combined)
        self.assertIn("maxSrtStreams survivor tokens cannot carry", combined)

    def test_policy_cannot_omit_baseline_admission_evidence(self):
        self.policy["requiredCriteria"].pop()
        self.policy["requiredArtifactKinds"].pop()
        self.save_manifests()

        check = check_scale_evidence(self.report_path, self.policy_path)

        combined = "\n".join(check.errors)
        self.assertIn("policy.requiredCriteria omits baseline values", combined)
        self.assertIn("policy.requiredArtifactKinds omits baseline values", combined)

    def test_repository_f5_policy_accepts_complete_fixture(self):
        policy_path = (
            Path(__file__).resolve().parents[1] / "scale/policies/f5-1000.json"
        )
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        self.evidence["acceptancePolicyVersion"] = policy["policyVersion"]
        self.report_path.write_text(
            json.dumps(self.evidence, sort_keys=True), encoding="utf-8"
        )

        check = check_scale_evidence(self.report_path, policy_path)

        self.assertTrue(check.passed, check.errors)


if __name__ == "__main__":
    unittest.main()
