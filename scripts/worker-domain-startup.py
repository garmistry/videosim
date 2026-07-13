#!/usr/bin/env python3
"""Boot and validate one F5 worker failure domain."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import ssl
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from videosim.host_identity import read_docker_host_identity


COMPOSE_FILE = ROOT / "docker-compose.worker-domain.yml"
WORKLOAD_FILE = ROOT / "scale/workloads/f5-1000-candidate.json"
WORKER_COUNT = 11
IMAGE_RE = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
CRITICAL_LOG_MARKERS = (
    "traceback (most recent call last)",
    "certificate verify failed",
    "verified worker identity does not match workerid",
    "report_spool=blocked",
    "report spool quota exhausted",
    "fatal:",
)


def candidate_worker_ids(failure_domain: str) -> tuple[str, ...]:
    workload = json.loads(WORKLOAD_FILE.read_text(encoding="utf-8"))
    zones = workload["placement"]["zones"]
    counts = workload["workerShape"]["failureDomainWorkerCounts"]
    if failure_domain not in zones:
        raise ValueError(
            "VIDEOSIM_FAILURE_DOMAIN must be one of " + ", ".join(zones)
        )
    index = zones.index(failure_domain)
    if counts[index] != WORKER_COUNT:
        raise ValueError(
            f"candidate workload must declare {WORKER_COUNT} workers for {failure_domain}"
        )
    return tuple(
        f"{failure_domain}-worker-{number:02d}"
        for number in range(1, WORKER_COUNT + 1)
    )


def _secure_mode(path: Path) -> str:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ValueError(f"private file must be mode 0600 or stricter: {path}")
    return f"{mode:04o}"


def _decode_certificate(path: Path) -> dict:
    try:
        certificate = ssl._ssl._test_decode_cert(str(path))
    except (OSError, ssl.SSLError) as exc:
        raise ValueError(f"certificate is invalid: {path}: {exc}") from exc
    expires = certificate.get("notAfter")
    if not expires or ssl.cert_time_to_seconds(expires) <= time.time():
        raise ValueError(f"certificate is expired or missing expiry: {path}")
    return certificate


def _common_name(certificate: dict) -> str:
    names = [
        value
        for relative_name in certificate.get("subject", ())
        for key, value in relative_name
        if key == "commonName"
    ]
    if len(names) != 1:
        raise ValueError("worker certificate must contain exactly one commonName")
    return names[0]


def validate_certificate_inventory(
    certificate_dir: Path, worker_ids: tuple[str, ...]
) -> list[dict]:
    if not certificate_dir.is_dir():
        raise ValueError(f"worker certificate directory does not exist: {certificate_dir}")
    ca_path = certificate_dir / "server-ca.crt"
    spool_key_path = certificate_dir / "worker-spool.key"
    if not ca_path.is_file():
        raise ValueError(f"worker server CA does not exist: {ca_path}")
    if not spool_key_path.is_file():
        raise ValueError(f"worker spool key does not exist: {spool_key_path}")
    _decode_certificate(ca_path)
    spool_mode = _secure_mode(spool_key_path)
    try:
        decoded_spool_key = base64.b64decode(
            spool_key_path.read_bytes().strip(), altchars=b"-_", validate=True
        )
    except (OSError, ValueError) as exc:
        raise ValueError(f"worker spool key is invalid: {spool_key_path}") from exc
    if len(decoded_spool_key) != 32:
        raise ValueError("worker spool key must decode to exactly 32 bytes")

    inventory = []
    for worker_id in worker_ids:
        certificate_path = certificate_dir / f"{worker_id}.crt"
        key_path = certificate_dir / f"{worker_id}.key"
        if not certificate_path.is_file() or not key_path.is_file():
            raise ValueError(f"worker TLS pair is incomplete for {worker_id}")
        certificate = _decode_certificate(certificate_path)
        if _common_name(certificate) != worker_id:
            raise ValueError(f"worker certificate CN does not match {worker_id}")
        key_mode = _secure_mode(key_path)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        try:
            context.load_cert_chain(certificate_path, key_path)
        except ssl.SSLError as exc:
            raise ValueError(f"worker certificate/key mismatch for {worker_id}") from exc
        inventory.append(
            {
                "workerId": worker_id,
                "certificateSha256": hashlib.sha256(
                    certificate_path.read_bytes()
                ).hexdigest(),
                "certificateExpires": certificate["notAfter"],
                "keyMode": key_mode,
                "spoolKeyMode": spool_mode,
            }
        )
    return inventory


def validate_container_records(
    records: list[dict], expected_services: set[str], image_id: str
) -> None:
    by_service = {record["service"]: record for record in records}
    if set(by_service) != expected_services:
        raise RuntimeError("worker container set does not match the 11-service domain")
    for service in sorted(expected_services):
        record = by_service[service]
        if record["imageId"] != image_id:
            raise RuntimeError(f"{service} is not running the resolved worker image")
        if not record["running"]:
            raise RuntimeError(f"{service} is not running")
        if record["restartCount"]:
            raise RuntimeError(f"{service} restarted {record['restartCount']} time(s)")


def critical_log_matches(log_output: str) -> list[str]:
    lowered = log_output.lower()
    return [marker for marker in CRITICAL_LOG_MARKERS if marker in lowered]


class WorkerDomainStartup:
    def __init__(self):
        self.failure_domain = os.environ["VIDEOSIM_FAILURE_DOMAIN"].strip()
        self.worker_ids = candidate_worker_ids(self.failure_domain)
        self.services = {f"worker-{number:02d}" for number in range(1, 12)}
        self.image = os.environ["VIDEOSIM_WORKER_IMAGE"].strip()
        self.certificate_dir = Path(
            os.environ["VIDEOSIM_WORKER_CERT_DIR"]
        ).resolve()
        self.data_dir = Path(os.environ["VIDEOSIM_WORKER_DATA_DIR"]).resolve()
        self.health_url = os.environ["VIDEOSIM_CONTROL_PLANE_HEALTH_URL"].strip()
        self.project = os.environ.get(
            "VIDEOSIM_WORKER_STARTUP_PROJECT",
            f"videosim-worker-{self.failure_domain}",
        )
        self.artifact_dir = Path(
            os.environ.get(
                "VIDEOSIM_WORKER_STARTUP_ARTIFACT_DIR",
                str(ROOT / "artifacts" / "worker-domain" / self.failure_domain),
            )
        ).resolve()
        self.no_pull = os.environ.get("VIDEOSIM_WORKER_STARTUP_NO_PULL") == "1"
        self.allow_mutable_image = (
            os.environ.get("VIDEOSIM_WORKER_STARTUP_ALLOW_MUTABLE_IMAGE") == "1"
        )
        self.stability_seconds = float(
            os.environ.get("VIDEOSIM_WORKER_STARTUP_STABILITY_SECONDS", "10")
        )
        if (
            not math.isfinite(self.stability_seconds)
            or not 0 <= self.stability_seconds <= 300
        ):
            raise ValueError(
                "VIDEOSIM_WORKER_STARTUP_STABILITY_SECONDS must be from 0 to 300"
            )
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.compose_command = [
            "docker",
            "compose",
            "-p",
            self.project,
            "-f",
            str(COMPOSE_FILE),
        ]

    def compose(self, *args: str, check: bool = True, **options):
        return subprocess.run(
            [*self.compose_command, *args],
            cwd=ROOT,
            env=os.environ.copy(),
            check=check,
            timeout=options.pop("timeout", 600),
            **options,
        )

    def wait_for(self, description: str, condition, timeout: float = 180):
        deadline = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < deadline:
            try:
                value = condition()
                if value:
                    return value
            except Exception as exc:
                last_error = exc
            time.sleep(1)
        detail = f": {last_error}" if last_error else ""
        raise RuntimeError(f"timed out waiting for {description}{detail}")

    def running_services(self) -> set[str]:
        result = self.compose(
            "ps",
            "--status",
            "running",
            "--services",
            text=True,
            capture_output=True,
            timeout=30,
        )
        return {line.strip() for line in result.stdout.splitlines() if line.strip()}

    def image_id(self) -> str:
        return subprocess.run(
            ["docker", "image", "inspect", self.image, "--format", "{{.Id}}"],
            check=True,
            text=True,
            capture_output=True,
            timeout=30,
        ).stdout.strip()

    def container_records(self, image_id: str) -> list[dict]:
        records = []
        for service in sorted(self.services):
            container_id = self.compose(
                "ps", "-q", service, text=True, capture_output=True, timeout=30
            ).stdout.strip()
            if not container_id:
                raise RuntimeError(f"{service} has no container")
            inspection = json.loads(
                subprocess.run(
                    ["docker", "inspect", container_id],
                    check=True,
                    text=True,
                    capture_output=True,
                    timeout=30,
                ).stdout
            )[0]
            records.append(
                {
                    "service": service,
                    "containerId": inspection["Id"],
                    "imageId": inspection["Image"],
                    "expectedImageId": image_id,
                    "running": inspection["State"]["Running"],
                    "restartCount": inspection["RestartCount"],
                    "startedAt": inspection["State"]["StartedAt"],
                }
            )
        return records

    def host_health_api(self) -> dict:
        context = None
        if self.health_url.startswith("https://"):
            context = ssl.create_default_context(
                cafile=str(self.certificate_dir / "server-ca.crt")
            )
        with urlopen(self.health_url, context=context, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("status") not in {"healthy", "ready"}:
            raise RuntimeError("control-plane health API did not report healthy")
        (self.artifact_dir / "control-plane-health.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        return payload

    def worker_health_api_ready(self) -> bool:
        worker_id = self.worker_ids[0]
        code = (
            "import json,os,ssl; from urllib.request import urlopen; "
            "u=os.environ['VIDEOSIM_CONTROL_PLANE_URL'].rstrip('/')+'/healthz'; "
            "c=ssl.create_default_context(cafile='/etc/videosim/certs/server-ca.crt'); "
            f"c.load_cert_chain('/etc/videosim/certs/{worker_id}.crt',"
            f"'/etc/videosim/certs/{worker_id}.key'); "
            "p=json.loads(urlopen(u,context=c,timeout=5).read()); "
            "raise SystemExit(p.get('status') not in {'healthy','ready'})"
        )
        result = self.compose(
            "exec",
            "-T",
            "worker-01",
            "python",
            "-c",
            code,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        return result.returncode == 0

    def capture_artifacts(self, records: list[dict] | None = None):
        commands = {
            "compose-ps.txt": ("ps", "--all"),
            "docker.log": ("logs", "--no-color", "--timestamps"),
            "compose-config.json": ("config", "--format", "json"),
        }
        for name, args in commands.items():
            with (self.artifact_dir / name).open("w", encoding="utf-8") as output:
                self.compose(
                    *args,
                    check=False,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    timeout=60,
                )
        if records is not None:
            (self.artifact_dir / "container-state.json").write_text(
                json.dumps(records, indent=2, sort_keys=True), encoding="utf-8"
            )

    def run(self) -> dict:
        result = {
            "passed": False,
            "startedAt": datetime.now(timezone.utc).isoformat(),
            "project": self.project,
            "failureDomain": self.failure_domain,
            "expectedWorkerIds": list(self.worker_ids),
            "workerImage": self.image,
            "immutableImage": bool(IMAGE_RE.fullmatch(self.image)),
            "checks": [],
            "errors": [],
        }
        records = None
        captured = False
        try:
            if not result["immutableImage"] and not self.allow_mutable_image:
                raise RuntimeError(
                    "VIDEOSIM_WORKER_IMAGE must use an immutable @sha256 digest"
                )
            if not self.data_dir.is_dir() or not os.access(self.data_dir, os.W_OK):
                raise RuntimeError(
                    f"worker data directory must exist and be writable: {self.data_dir}"
                )
            inventory = validate_certificate_inventory(
                self.certificate_dir, self.worker_ids
            )
            (self.artifact_dir / "certificate-inventory.json").write_text(
                json.dumps(inventory, indent=2, sort_keys=True), encoding="utf-8"
            )
            result["checks"].append("candidate_configuration_validated")
            result["checks"].append("worker_certificates_validated")

            self.compose("version", timeout=30)
            result["hostIdentity"] = read_docker_host_identity()
            result["checks"].append("linux_docker_engine")
            self.compose("config", "--quiet", timeout=30)
            result["checks"].append("compose_rendered")
            if not self.no_pull:
                self.compose("pull")
                result["checks"].append("immutable_image_pulled")
            resolved_image_id = self.image_id()
            result["resolvedImageId"] = resolved_image_id

            self.compose("up", "-d", "--no-build")
            self.wait_for(
                "all worker services", lambda: self.running_services() == self.services
            )
            result["checks"].append("worker_services_running")
            self.wait_for("host control-plane health API", self.host_health_api)
            result["checks"].append("control_plane_health_api")
            self.wait_for("worker mTLS health API", self.worker_health_api_ready)
            result["checks"].append("worker_mtls_health_api")

            time.sleep(self.stability_seconds)
            if self.running_services() != self.services:
                raise RuntimeError("worker service set changed during stability window")
            records = self.container_records(resolved_image_id)
            validate_container_records(records, self.services, resolved_image_id)
            result["checks"].append("worker_services_stable")

            self.capture_artifacts(records)
            captured = True
            log_output = (self.artifact_dir / "docker.log").read_text(
                encoding="utf-8"
            )
            if not log_output.strip():
                raise RuntimeError("worker Docker logs are empty")
            markers = critical_log_matches(log_output)
            if markers:
                raise RuntimeError(
                    "worker Docker logs contain critical markers: "
                    + ", ".join(markers)
                )
            result["checks"].append("docker_logs_clean")
            result["passed"] = True
        except Exception as exc:
            result["errors"].append(str(exc))
        finally:
            if not captured:
                try:
                    self.capture_artifacts(records)
                except Exception as exc:
                    result["errors"].append(f"artifact capture failed: {exc}")
            result["endedAt"] = datetime.now(timezone.utc).isoformat()
            (self.artifact_dir / "result.json").write_text(
                json.dumps(result, indent=2, sort_keys=True), encoding="utf-8"
            )
        return result


def main() -> int:
    try:
        startup = WorkerDomainStartup()
    except Exception as exc:
        print(f"Worker domain startup FAIL: {exc}", file=sys.stderr)
        return 1
    result = startup.run()
    print(json.dumps(result, indent=2, sort_keys=True))
    print(f"Artifacts: {startup.artifact_dir}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
