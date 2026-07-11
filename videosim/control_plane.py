from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Mapping, Sequence


WORKER_API_VERSION = "videosim.worker/v1"


class WorkerContractError(ValueError):
    """Base error for invalid worker/control-plane messages."""


class WorkerReportConflict(WorkerContractError):
    """The report was produced for a stale or different assignment."""


class WorkerReportValidationError(WorkerContractError):
    """The report does not satisfy the worker report schema."""


class WorkerReportPersistenceError(WorkerContractError):
    """The control plane could not durably apply an accepted report."""


@dataclass(frozen=True)
class AssignmentContract:
    api_version: str
    control_plane_instance_id: str
    assignment_generation: int
    assignment_token: str
    worker_id: str
    stream_ids: tuple[str, ...]

    def payload(self) -> dict:
        return {
            "apiVersion": self.api_version,
            "controlPlaneInstanceId": self.control_plane_instance_id,
            "assignmentGeneration": self.assignment_generation,
            "assignmentToken": self.assignment_token,
        }


def assignment_fingerprint(worker_ids: Sequence[str], stream_contracts: Sequence[Mapping]) -> str:
    canonical = json.dumps(
        {"workers": list(worker_ids), "streams": list(stream_contracts)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def assignment_token(
    control_plane_instance_id: str,
    assignment_generation: int,
    worker_id: str,
    stream_contracts: Sequence[Mapping],
) -> str:
    """Return an opaque integrity token for one process-local assignment.

    This is deliberately not an authentication credential. Durable, signed
    leases replace it in the production control-plane milestone.
    """

    canonical = json.dumps(
        {
            "apiVersion": WORKER_API_VERSION,
            "controlPlaneInstanceId": control_plane_instance_id,
            "assignmentGeneration": int(assignment_generation),
            "workerId": worker_id,
            "streams": list(stream_contracts),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def validate_report_contract(
    current: AssignmentContract,
    report: Mapping,
    *,
    allow_legacy: bool,
) -> bool:
    """Validate report fencing metadata.

    Returns True for an explicitly permitted legacy report. A conflict is
    raised before any monitor state is mutated.
    """

    fields = (
        "apiVersion",
        "controlPlaneInstanceId",
        "assignmentGeneration",
        "assignmentToken",
    )
    supplied = [field in report for field in fields]
    if not any(supplied):
        if allow_legacy:
            return True
        raise WorkerReportConflict("unversioned worker reports are disabled")
    if not all(supplied):
        raise WorkerReportValidationError("worker report contract metadata must be complete")
    if report.get("apiVersion") != current.api_version:
        raise WorkerReportConflict("worker API version does not match the current assignment")
    if report.get("controlPlaneInstanceId") != current.control_plane_instance_id:
        raise WorkerReportConflict("control-plane instance does not match the current assignment")
    generation = report.get("assignmentGeneration")
    if type(generation) is not int:
        raise WorkerReportValidationError("assignmentGeneration must be a JSON integer")
    if generation != current.assignment_generation:
        raise WorkerReportConflict("assignment generation is stale")
    if report.get("assignmentToken") != current.assignment_token:
        raise WorkerReportConflict("assignment token does not match current ownership")
    return False


def validate_monitor_items(worker_state: Mapping, accepted_stream_ids: set[str]) -> tuple[dict, dict]:
    """Validate and scope every worker monitor-state item.

    The returned state contains only in-scope items. Out-of-scope items are
    reported explicitly rather than silently granting authority.
    """

    if not isinstance(worker_state, Mapping):
        raise WorkerReportValidationError("state must be an object")

    scoped = {
        "updatedAt": str(worker_state.get("updatedAt", "")),
        "alarms": [],
        "events": [],
        "pending": [],
    }
    dropped: dict[str, list[str]] = {"alarms": [], "events": [], "pending": [], "probeMetrics": []}
    for collection in ("alarms", "events", "pending"):
        items = worker_state.get(collection, [])
        if not isinstance(items, list):
            raise WorkerReportValidationError(f"state.{collection} must be an array")
        for index, item in enumerate(items):
            if not isinstance(item, Mapping):
                raise WorkerReportValidationError(f"state.{collection}[{index}] must be an object")
            stream_id = item.get("streamId")
            if not isinstance(stream_id, str) or not stream_id:
                raise WorkerReportValidationError(f"state.{collection}[{index}].streamId is required")
            if stream_id in accepted_stream_ids:
                scoped[collection].append(dict(item))
            else:
                dropped[collection].append(str(item.get("id", f"{collection}[{index}]")))

    metrics = worker_state.get("probeMetrics")
    if metrics is not None:
        if not isinstance(metrics, Mapping):
            raise WorkerReportValidationError("state.probeMetrics must be an object")
        metric_items = metrics.get("streams", [])
        if not isinstance(metric_items, list):
            raise WorkerReportValidationError("state.probeMetrics.streams must be an array")
        accepted_metrics = []
        for index, item in enumerate(metric_items):
            if not isinstance(item, Mapping):
                raise WorkerReportValidationError(f"state.probeMetrics.streams[{index}] must be an object")
            stream_id = item.get("streamId")
            if not isinstance(stream_id, str) or not stream_id:
                raise WorkerReportValidationError(f"state.probeMetrics.streams[{index}].streamId is required")
            if stream_id in accepted_stream_ids:
                accepted_metrics.append(dict(item))
            else:
                dropped["probeMetrics"].append(f"{stream_id}:{item.get('check', index)}")
        outcomes: dict[str, int] = {}
        for item in accepted_metrics:
            outcome = str(item.get("outcome", "unknown"))
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
        scoped["probeMetrics"] = {
            "observedAt": str(metrics.get("observedAt", "")),
            "batchDurationMs": metrics.get("batchDurationMs", 0),
            "streamCount": len({item["streamId"] for item in accepted_metrics}),
            "checkCount": len(accepted_metrics),
            "outcomes": outcomes,
            "streams": accepted_metrics,
        }
    return scoped, dropped
