from __future__ import annotations

import json
import subprocess


HOST_IDENTITY_SCHEMA = "videosim.docker-host-identity/v1"
STRING_FIELDS = (
    "dockerEngineId",
    "dockerName",
    "operatingSystem",
    "kernelVersion",
    "architecture",
    "serverVersion",
)


def validate_host_identity(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("host identity must be an object")
    if value.get("schemaVersion") != HOST_IDENTITY_SCHEMA:
        raise ValueError(f"host identity schemaVersion must be {HOST_IDENTITY_SCHEMA}")
    if value.get("osType") != "linux":
        raise ValueError("host identity must describe a Linux Docker engine")
    for field in STRING_FIELDS:
        item = value.get(field)
        if not isinstance(item, str) or not item.strip() or len(item) > 256:
            raise ValueError(f"host identity {field} must be a bounded non-empty string")
    for field in ("logicalCpus", "memoryBytes"):
        item = value.get(field)
        if isinstance(item, bool) or not isinstance(item, int) or item < 1:
            raise ValueError(f"host identity {field} must be a positive integer")
    return {
        "schemaVersion": HOST_IDENTITY_SCHEMA,
        "dockerEngineId": value["dockerEngineId"],
        "dockerName": value["dockerName"],
        "osType": "linux",
        "operatingSystem": value["operatingSystem"],
        "kernelVersion": value["kernelVersion"],
        "architecture": value["architecture"],
        "logicalCpus": value["logicalCpus"],
        "memoryBytes": value["memoryBytes"],
        "serverVersion": value["serverVersion"],
    }


def host_identity_from_docker_info(info: object) -> dict:
    if not isinstance(info, dict):
        raise ValueError("Docker info must be an object")
    return validate_host_identity(
        {
            "schemaVersion": HOST_IDENTITY_SCHEMA,
            "dockerEngineId": info.get("ID"),
            "dockerName": info.get("Name"),
            "osType": info.get("OSType"),
            "operatingSystem": info.get("OperatingSystem"),
            "kernelVersion": info.get("KernelVersion"),
            "architecture": info.get("Architecture"),
            "logicalCpus": info.get("NCPU"),
            "memoryBytes": info.get("MemTotal"),
            "serverVersion": info.get("ServerVersion"),
        }
    )


def read_docker_host_identity() -> dict:
    result = subprocess.run(
        ["docker", "info", "--format", "{{json .}}"],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
    )
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Docker info is not valid JSON: {exc}") from exc
    return host_identity_from_docker_info(info)
