from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


WORKLOAD_SCHEMA = "videosim.scale-workload/v1"
EVIDENCE_SCHEMA = "videosim.scale-evidence/v1"
POLICY_SCHEMA = "videosim.capacity-policy/v1"

ADMISSION_CRITERIA = (
    "assignment-correctness",
    "bounded-failover",
    "no-duplicate-authority",
    "alarm-consistency",
    "backpressure",
    "sustained-load",
    "security",
    "durability",
    "operability",
    "rollback",
)

BASELINE_ARTIFACT_KINDS = (
    "change-manifest",
    "migration-manifest",
    "raw-metrics",
    "test-results",
    "docker-logs",
    "dashboard-export",
    "security-report",
    "restore-report",
    "rollback-record",
)
F5_DOMAIN_PREFLIGHT_ARTIFACT_KIND = "f5-domain-preflight"
F5_DOMAIN_PREFLIGHT_SCHEMA = "videosim.f5-domain-preflight/v1"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40,64}$")


@dataclass(frozen=True)
class ScaleEvidenceCheck:
    errors: tuple[str, ...]
    target_streams: int = 0
    load_streams: int = 0
    verified_artifacts: int = 0

    @property
    def passed(self) -> bool:
        return not self.errors

    def payload(self) -> dict:
        return {
            "schemaVersion": "videosim.capacity-check/v1",
            "passed": self.passed,
            "targetStreams": self.target_streams,
            "loadStreams": self.load_streams,
            "verifiedArtifacts": self.verified_artifacts,
            "errors": list(self.errors),
        }

    def to_json(self) -> str:
        return json.dumps(self.payload(), sort_keys=True)


def _required(obj: dict, fields: tuple[str, ...], label: str, errors: list[str]):
    for field in fields:
        if field not in obj:
            errors.append(f"{label}.{field} is required")


def _positive_int(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


def _non_negative_int(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _finite_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def _string_list(value: object, *, allow_empty: bool = False) -> bool:
    return (
        isinstance(value, list)
        and (allow_empty or bool(value))
        and all(isinstance(item, str) and item.strip() for item in value)
    )


def _read_object(path: Path, label: str, errors: list[str]) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append(f"{label} could not be read as JSON: {exc}")
        return None
    if not isinstance(payload, dict):
        errors.append(f"{label} must be a JSON object")
        return None
    return payload


def _validate_policy(policy: dict, errors: list[str]):
    _required(
        policy,
        (
            "schemaVersion",
            "policyVersion",
            "targetStreams",
            "minimumDurationSeconds",
            "minimumHeadroomPercent",
            "minimumUnavailableFailureDomains",
            "requireCleanSource",
            "allowSkippedChecks",
            "requiredCriteria",
            "requiredArtifactKinds",
        ),
        "policy",
        errors,
    )
    if policy.get("schemaVersion") != POLICY_SCHEMA:
        errors.append(f"policy.schemaVersion must be {POLICY_SCHEMA}")
    if not isinstance(policy.get("policyVersion"), str) or not policy.get(
        "policyVersion", ""
    ).strip():
        errors.append("policy.policyVersion must be a non-empty string")
    for field in (
        "targetStreams",
        "minimumDurationSeconds",
        "minimumUnavailableFailureDomains",
    ):
        if not _positive_int(policy.get(field)):
            errors.append(f"policy.{field} must be a positive integer")
    headroom = policy.get("minimumHeadroomPercent")
    if not _finite_number(headroom) or not 0 <= headroom <= 100:
        errors.append(
            "policy.minimumHeadroomPercent must be a finite number from 0 to 100"
        )
    for field in ("requireCleanSource", "allowSkippedChecks"):
        if not isinstance(policy.get(field), bool):
            errors.append(f"policy.{field} must be a boolean")
    for field, baseline in (
        ("requiredCriteria", ADMISSION_CRITERIA),
        ("requiredArtifactKinds", BASELINE_ARTIFACT_KINDS),
    ):
        values = policy.get(field)
        if not _string_list(values):
            errors.append(f"policy.{field} must be a non-empty string array")
            continue
        if len(values) != len(set(values)):
            errors.append(f"policy.{field} must not contain duplicates")
        missing = sorted(set(baseline) - set(values))
        if missing:
            errors.append(f"policy.{field} omits baseline values: {', '.join(missing)}")


def _validate_mix(
    value: object, fields: tuple[str, ...], label: str, errors: list[str]
):
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return
    _required(value, fields, label, errors)
    percentages = [value.get(field) for field in fields]
    if not all(_finite_number(item) and 0 <= item <= 100 for item in percentages):
        errors.append(f"{label} percentages must be finite numbers from 0 to 100")
    elif not math.isclose(sum(percentages), 100, abs_tol=1e-6):
        errors.append(f"{label} percentages must total 100")


def _validate_percentiles(value: object, label: str, errors: list[str]):
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return
    _required(value, ("p50", "p95", "p99"), label, errors)
    samples = [value.get(field) for field in ("p50", "p95", "p99")]
    if not all(_finite_number(item) and item >= 0 for item in samples):
        errors.append(f"{label} percentiles must be finite non-negative numbers")
    elif samples != sorted(samples):
        errors.append(f"{label} percentiles must satisfy p50 <= p95 <= p99")


def _validate_workload(workload: dict, policy: dict, errors: list[str]):
    _required(
        workload,
        (
            "schemaVersion",
            "randomSeed",
            "targetStreams",
            "loadStreams",
            "headroomPercent",
            "failureDomainsUnavailable",
            "protocolMix",
            "sourceMix",
            "placement",
            "checkProfiles",
            "endpointDistribution",
            "eventStorms",
            "workerShape",
            "infrastructureVersions",
            "durationSeconds",
            "expectedInvariants",
        ),
        "workload",
        errors,
    )
    if workload.get("schemaVersion") != WORKLOAD_SCHEMA:
        errors.append(f"workload.schemaVersion must be {WORKLOAD_SCHEMA}")
    if not _non_negative_int(workload.get("randomSeed")):
        errors.append("workload.randomSeed must be a non-negative integer")
    for field in ("targetStreams", "loadStreams", "failureDomainsUnavailable", "durationSeconds"):
        if not _positive_int(workload.get(field)):
            errors.append(f"workload.{field} must be a positive integer")

    target = workload.get("targetStreams")
    policy_target = policy.get("targetStreams")
    if _positive_int(target) and _positive_int(policy_target) and target != policy_target:
        errors.append("workload.targetStreams does not match policy.targetStreams")
    headroom = workload.get("headroomPercent")
    minimum_headroom = policy.get("minimumHeadroomPercent")
    if not _finite_number(headroom) or not 0 <= headroom <= 100:
        errors.append("workload.headroomPercent must be a finite number from 0 to 100")
    elif _finite_number(minimum_headroom) and headroom < minimum_headroom:
        errors.append("workload.headroomPercent is below policy minimum")
    load = workload.get("loadStreams")
    if _positive_int(target) and _positive_int(load) and _finite_number(minimum_headroom):
        required_load = math.ceil(target * (1 + minimum_headroom / 100))
        if load < required_load:
            errors.append(
                f"workload.loadStreams must be at least {required_load} for policy headroom"
            )
    unavailable = workload.get("failureDomainsUnavailable")
    minimum_unavailable = policy.get("minimumUnavailableFailureDomains")
    if (
        _positive_int(unavailable)
        and _positive_int(minimum_unavailable)
        and unavailable < minimum_unavailable
    ):
        errors.append("workload.failureDomainsUnavailable is below policy minimum")
    duration = workload.get("durationSeconds")
    minimum_duration = policy.get("minimumDurationSeconds")
    if (
        _positive_int(duration)
        and _positive_int(minimum_duration)
        and duration < minimum_duration
    ):
        errors.append("workload.durationSeconds is below policy minimum")

    protocol_mix = workload.get("protocolMix")
    _validate_mix(protocol_mix, ("srtPercent", "dashPercent"), "workload.protocolMix", errors)
    _validate_mix(workload.get("sourceMix"), ("generatedPercent", "externalPercent"), "workload.sourceMix", errors)

    placement = workload.get("placement")
    if not isinstance(placement, dict):
        errors.append("workload.placement must be an object")
    else:
        for field in ("regions", "zones"):
            if not _string_list(placement.get(field)):
                errors.append(f"workload.placement.{field} must be a non-empty string array")

    profiles = workload.get("checkProfiles")
    if not isinstance(profiles, list) or not profiles:
        errors.append("workload.checkProfiles must be a non-empty array")
    else:
        for index, profile in enumerate(profiles):
            if not isinstance(profile, dict):
                errors.append(f"workload.checkProfiles[{index}] must be an object")
                continue
            if not isinstance(profile.get("name"), str) or not profile.get("name", "").strip():
                errors.append(f"workload.checkProfiles[{index}].name is required")
            if not _positive_int(profile.get("cadenceSeconds")):
                errors.append(
                    f"workload.checkProfiles[{index}].cadenceSeconds must be positive"
                )

    endpoint = workload.get("endpointDistribution")
    if not isinstance(endpoint, dict):
        errors.append("workload.endpointDistribution must be an object")
    else:
        _validate_mix(
            endpoint,
            ("healthyPercent", "slowPercent", "deadPercent", "malformedPercent"),
            "workload.endpointDistribution",
            errors,
        )
        _validate_percentiles(
            endpoint.get("latencyMs"),
            "workload.endpointDistribution.latencyMs",
            errors,
        )

    storms = workload.get("eventStorms")
    if not isinstance(storms, list) or not storms:
        errors.append("workload.eventStorms must be a non-empty array")
    else:
        for index, storm in enumerate(storms):
            if not isinstance(storm, dict):
                errors.append(f"workload.eventStorms[{index}] must be an object")
                continue
            if not _non_negative_int(storm.get("offsetSeconds")):
                errors.append(
                    f"workload.eventStorms[{index}].offsetSeconds must be non-negative"
                )
            if not isinstance(storm.get("kind"), str) or not storm.get("kind", "").strip():
                errors.append(f"workload.eventStorms[{index}].kind is required")
    shape = workload.get("workerShape")
    if not isinstance(shape, dict):
        errors.append("workload.workerShape must be an object")
    else:
        for field in (
            "count",
            "failureDomains",
            "maxStreams",
            "maxSrtStreams",
            "maxDashStreams",
            "maxConcurrentChecks",
            "maxConcurrentDeepChecks",
        ):
            if not _positive_int(shape.get(field)):
                errors.append(f"workload.workerShape.{field} must be a positive integer")
        domain_counts = shape.get("failureDomainWorkerCounts")
        if not isinstance(domain_counts, list) or not domain_counts or not all(
            _positive_int(item) for item in domain_counts
        ):
            errors.append(
                "workload.workerShape.failureDomainWorkerCounts must be a non-empty positive-integer array"
            )
        elif _positive_int(shape.get("count")) and sum(domain_counts) != shape["count"]:
            errors.append(
                "workload.workerShape.failureDomainWorkerCounts must total worker count"
            )
        if (
            isinstance(domain_counts, list)
            and _positive_int(shape.get("failureDomains"))
            and len(domain_counts) != shape["failureDomains"]
        ):
            errors.append(
                "workload.workerShape.failureDomains must match failureDomainWorkerCounts"
            )
        if (
            _positive_int(shape.get("failureDomains"))
            and _positive_int(unavailable)
            and shape["failureDomains"] <= unavailable
        ):
            errors.append(
                "workload.workerShape.failureDomains must exceed unavailable domains"
            )
        if (
            isinstance(domain_counts, list)
            and domain_counts
            and all(_positive_int(item) for item in domain_counts)
            and _positive_int(unavailable)
            and unavailable < len(domain_counts)
            and _positive_int(load)
            and isinstance(protocol_mix, dict)
            and all(
                _finite_number(protocol_mix.get(field))
                for field in ("srtPercent", "dashPercent")
            )
            and all(
                _positive_int(shape.get(field))
                for field in ("count", "maxStreams", "maxSrtStreams", "maxDashStreams")
            )
        ):
            surviving_workers = shape["count"] - sum(
                sorted(domain_counts, reverse=True)[:unavailable]
            )
            required_by_protocol = {
                "maxSrtStreams": math.ceil(
                    load * protocol_mix["srtPercent"] / 100
                ),
                "maxDashStreams": math.ceil(
                    load * protocol_mix["dashPercent"] / 100
                ),
            }
            if surviving_workers * shape["maxStreams"] < load:
                errors.append(
                    "workload.workerShape total survivor tokens cannot carry loadStreams"
                )
            for field, required in required_by_protocol.items():
                if surviving_workers * shape[field] < required:
                    errors.append(
                        f"workload.workerShape {field} survivor tokens cannot carry protocol mix"
                    )
    versions = workload.get("infrastructureVersions")
    if not isinstance(versions, dict) or not versions or not all(
        isinstance(key, str)
        and key.strip()
        and isinstance(value, str)
        and value.strip()
        for key, value in versions.items()
    ):
        errors.append("workload.infrastructureVersions must be a non-empty string map")
    if not _string_list(workload.get("expectedInvariants")):
        errors.append("workload.expectedInvariants must be a non-empty string array")


def _parse_timestamp(value: object, label: str, errors: list[str]) -> datetime | None:
    if not isinstance(value, str):
        errors.append(f"{label} must be an ISO-8601 timestamp")
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"{label} must be an ISO-8601 timestamp")
        return None
    if parsed.tzinfo is None:
        errors.append(f"{label} must include a timezone")
        return None
    return parsed


def _verify_artifact(
    base: Path,
    reference: object,
    label: str,
    seen_paths: set[Path],
    errors: list[str],
) -> Path | None:
    if not isinstance(reference, dict):
        errors.append(f"{label} must be an object")
        return None
    _required(reference, ("path", "sha256"), label, errors)
    relative = reference.get("path")
    expected = reference.get("sha256")
    if not isinstance(relative, str) or not relative.strip():
        errors.append(f"{label}.path must be a non-empty relative path")
        return None
    candidate = Path(relative)
    if candidate.is_absolute():
        errors.append(f"{label}.path must be relative to the evidence report")
        return None
    resolved = (base / candidate).resolve()
    if resolved != base and base not in resolved.parents:
        errors.append(f"{label}.path escapes the evidence directory")
        return None
    if resolved in seen_paths:
        errors.append(f"{label}.path duplicates another artifact")
        return None
    seen_paths.add(resolved)
    if not isinstance(expected, str) or not _SHA256.fullmatch(expected):
        errors.append(f"{label}.sha256 must be a lowercase SHA-256 digest")
        return None
    try:
        actual = hashlib.sha256(resolved.read_bytes()).hexdigest()
    except OSError as exc:
        errors.append(f"{label}.path could not be read: {exc}")
        return None
    if actual != expected:
        errors.append(f"{label}.sha256 does not match {relative}")
        return None
    return resolved


def _validate_f5_domain_preflight(
    value: object,
    workload: dict | None,
    workload_sha256: str | None,
    image_digests: set[str],
    errors: list[str],
):
    label = F5_DOMAIN_PREFLIGHT_ARTIFACT_KIND
    if not isinstance(value, dict):
        errors.append(f"{label} must be a JSON object")
        return
    if value.get("schemaVersion") != F5_DOMAIN_PREFLIGHT_SCHEMA:
        errors.append(f"{label}.schemaVersion must be {F5_DOMAIN_PREFLIGHT_SCHEMA}")
    if value.get("passed") is not True or value.get("errors") != []:
        errors.append(f"{label} did not pass cleanly")
    if workload is None:
        errors.append(f"{label} cannot be matched without a valid workload")
        return
    shape = workload.get("workerShape")
    shape = shape if isinstance(shape, dict) else {}
    expected = {
        "targetStreams": workload.get("targetStreams"),
        "loadStreams": workload.get("loadStreams"),
        "fixtureDomains": shape.get("failureDomains"),
        "workerDomains": shape.get("failureDomains"),
        "workerCount": shape.get("count"),
        "workerRegistrationCount": shape.get("count"),
        "workerIncarnationCount": shape.get("count"),
        "workerResourceSnapshotCount": shape.get("failureDomains"),
    }
    for field, expected_value in expected.items():
        if expected_value is not None and value.get(field) != expected_value:
            errors.append(f"{label}.{field} does not match the workload")
    failure_domains = shape.get("failureDomains")
    if _positive_int(failure_domains) and value.get("dockerHostCount") != failure_domains * 2:
        errors.append(f"{label}.dockerHostCount does not match the failure domains")
    if value.get("distinctDockerHostsValidated") is not True:
        errors.append(f"{label}.distinctDockerHostsValidated must be true")
    input_sha256 = value.get("inputSha256")
    if (
        not isinstance(input_sha256, dict)
        or input_sha256.get("workload") != workload_sha256
    ):
        errors.append(f"{label}.inputSha256.workload does not match the evidence workload")
    if value.get("imageDigest") not in image_digests:
        errors.append(f"{label}.imageDigest does not match an evidence container image")


def check_scale_evidence(
    report_path: str | Path, policy_path: str | Path
) -> ScaleEvidenceCheck:
    errors: list[str] = []
    report_file = Path(report_path).resolve()
    policy = _read_object(Path(policy_path).resolve(), "policy", errors)
    evidence = _read_object(report_file, "evidence", errors)
    if policy is None or evidence is None:
        return ScaleEvidenceCheck(tuple(errors))
    _validate_policy(policy, errors)

    _required(
        evidence,
        (
            "schemaVersion",
            "sourceCommit",
            "dirtySource",
            "containerImages",
            "inputs",
            "startedAt",
            "endedAt",
            "command",
            "exitStatus",
            "artifacts",
            "metrics",
            "acceptancePolicyVersion",
            "criteria",
            "skippedChecks",
            "approvingOwner",
        ),
        "evidence",
        errors,
    )
    if evidence.get("schemaVersion") != EVIDENCE_SCHEMA:
        errors.append(f"evidence.schemaVersion must be {EVIDENCE_SCHEMA}")
    source_commit = evidence.get("sourceCommit")
    if not isinstance(source_commit, str) or not _COMMIT.fullmatch(source_commit):
        errors.append("evidence.sourceCommit must be a 40-64 character lowercase hex commit")
    dirty = evidence.get("dirtySource")
    if not isinstance(dirty, bool):
        errors.append("evidence.dirtySource must be a boolean")
    elif policy.get("requireCleanSource") is True and dirty:
        errors.append("evidence.dirtySource must be false for this policy")

    images = evidence.get("containerImages")
    image_digests: set[str] = set()
    if not isinstance(images, list) or not images:
        errors.append("evidence.containerImages must be a non-empty array")
    else:
        for index, item in enumerate(images):
            if not isinstance(item, dict):
                errors.append(f"evidence.containerImages[{index}] must be an object")
                continue
            if not isinstance(item.get("name"), str) or not item.get("name", "").strip():
                errors.append(f"evidence.containerImages[{index}].name is required")
            digest = item.get("digest")
            if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                errors.append(
                    f"evidence.containerImages[{index}].digest must be a sha256 digest"
                )
            else:
                image_digests.add(digest)

    if evidence.get("acceptancePolicyVersion") != policy.get("policyVersion"):
        errors.append("evidence.acceptancePolicyVersion does not match policy")
    if not _string_list(evidence.get("command")):
        errors.append("evidence.command must be a non-empty string array")
    exit_status = evidence.get("exitStatus")
    if isinstance(exit_status, bool) or not isinstance(exit_status, int):
        errors.append("evidence.exitStatus must be an integer")
    elif exit_status != 0:
        errors.append("evidence.exitStatus must be zero")
    if not isinstance(evidence.get("approvingOwner"), str) or not evidence.get(
        "approvingOwner", ""
    ).strip():
        errors.append("evidence.approvingOwner must be a non-empty string")

    skipped = evidence.get("skippedChecks")
    if not _string_list(skipped, allow_empty=True):
        errors.append("evidence.skippedChecks must be a string array")
    elif skipped and policy.get("allowSkippedChecks") is False:
        errors.append("evidence.skippedChecks must be empty for this policy")

    criteria = evidence.get("criteria")
    criteria_by_id: dict[str, dict] = {}
    if not isinstance(criteria, list):
        errors.append("evidence.criteria must be an array")
    else:
        for index, item in enumerate(criteria):
            if not isinstance(item, dict):
                errors.append(f"evidence.criteria[{index}] must be an object")
                continue
            criterion_id = item.get("id")
            if not isinstance(criterion_id, str) or not criterion_id.strip():
                errors.append(f"evidence.criteria[{index}].id is required")
                continue
            if criterion_id in criteria_by_id:
                errors.append(f"evidence.criteria contains duplicate id {criterion_id}")
            criteria_by_id[criterion_id] = item
            if not isinstance(item.get("passed"), bool):
                errors.append(f"evidence.criteria[{index}].passed must be a boolean")
            if not isinstance(item.get("detail"), str) or not item.get("detail", "").strip():
                errors.append(f"evidence.criteria[{index}].detail is required")
    required_criteria = policy.get("requiredCriteria", [])
    if isinstance(required_criteria, list):
        for criterion_id in required_criteria:
            item = criteria_by_id.get(criterion_id)
            if item is None:
                errors.append(f"evidence.criteria is missing {criterion_id}")
            elif item.get("passed") is not True:
                errors.append(f"evidence.criteria {criterion_id} did not pass")

    metrics = evidence.get("metrics")
    if not isinstance(metrics, dict) or not metrics:
        errors.append("evidence.metrics must be a non-empty object")
    else:
        for metric, summary in metrics.items():
            _validate_percentiles(summary, f"evidence.metrics.{metric}", errors)

    started = _parse_timestamp(evidence.get("startedAt"), "evidence.startedAt", errors)
    ended = _parse_timestamp(evidence.get("endedAt"), "evidence.endedAt", errors)
    if started is not None and ended is not None:
        elapsed = (ended - started).total_seconds()
        minimum_duration = policy.get("minimumDurationSeconds")
        if elapsed <= 0:
            errors.append("evidence.endedAt must be after evidence.startedAt")
        elif _positive_int(minimum_duration) and elapsed < minimum_duration:
            errors.append("evidence timestamp duration is below policy minimum")

    base = report_file.parent
    seen_paths: set[Path] = set()
    verified = 0
    workload: dict | None = None
    workload_sha256: str | None = None
    inputs = evidence.get("inputs")
    if not isinstance(inputs, dict):
        errors.append("evidence.inputs must be an object")
    else:
        _required(inputs, ("infrastructure", "config", "workload"), "evidence.inputs", errors)
        for name in ("infrastructure", "config", "workload"):
            resolved = _verify_artifact(
                base, inputs.get(name), f"evidence.inputs.{name}", seen_paths, errors
            )
            if resolved is not None:
                verified += 1
                if name == "workload":
                    workload = _read_object(resolved, "workload", errors)
                    reference = inputs.get(name)
                    if isinstance(reference, dict) and isinstance(
                        reference.get("sha256"), str
                    ):
                        workload_sha256 = reference["sha256"]

    artifact_kinds: set[str] = set()
    artifact_paths: dict[str, Path] = {}
    artifacts = evidence.get("artifacts")
    if not isinstance(artifacts, list):
        errors.append("evidence.artifacts must be an array")
    else:
        for index, item in enumerate(artifacts):
            label = f"evidence.artifacts[{index}]"
            if not isinstance(item, dict):
                errors.append(f"{label} must be an object")
                continue
            kind = item.get("kind")
            if not isinstance(kind, str) or not kind.strip():
                errors.append(f"{label}.kind is required")
            elif kind in artifact_kinds:
                errors.append(f"evidence.artifacts contains duplicate kind {kind}")
            else:
                artifact_kinds.add(kind)
            resolved = _verify_artifact(base, item, label, seen_paths, errors)
            if resolved is not None:
                verified += 1
                if isinstance(kind, str) and kind not in artifact_paths:
                    artifact_paths[kind] = resolved
    required_kinds = policy.get("requiredArtifactKinds", [])
    if isinstance(required_kinds, list):
        missing = sorted(set(required_kinds) - artifact_kinds)
        if missing:
            errors.append(f"evidence.artifacts omits required kinds: {', '.join(missing)}")

    target_streams = 0
    load_streams = 0
    if workload is not None:
        _validate_workload(workload, policy, errors)
        if _positive_int(workload.get("targetStreams")):
            target_streams = workload["targetStreams"]
        if _positive_int(workload.get("loadStreams")):
            load_streams = workload["loadStreams"]
        if started is not None and ended is not None and _positive_int(
            workload.get("durationSeconds")
        ):
            if (ended - started).total_seconds() < workload["durationSeconds"]:
                errors.append("evidence timestamp duration is below workload duration")

    preflight_path = artifact_paths.get(F5_DOMAIN_PREFLIGHT_ARTIFACT_KIND)
    if preflight_path is not None:
        _validate_f5_domain_preflight(
            _read_object(preflight_path, F5_DOMAIN_PREFLIGHT_ARTIFACT_KIND, errors),
            workload,
            workload_sha256,
            image_digests,
            errors,
        )

    return ScaleEvidenceCheck(
        tuple(errors),
        target_streams=target_streams,
        load_streams=load_streams,
        verified_artifacts=verified,
    )


def human_summary(check: ScaleEvidenceCheck) -> str:
    lines = [
        f"Scale evidence: {'PASS' if check.passed else 'FAIL'}",
        f"Target/load streams: {check.target_streams}/{check.load_streams}",
        f"Verified artifacts: {check.verified_artifacts}",
    ]
    lines.extend(f"- {error}" for error in check.errors)
    return "\n".join(lines)
