#!/usr/bin/env python3
"""Fault and recover one running distributed fixture domain."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "docker-compose.fixture-domain.yml"
SERVICES = {"srt", "dash"}
CRITICAL_LOG_MARKERS = (
    "traceback (most recent call last)",
    "fixture process exited",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_media_outcomes(payload: dict, *, healthy: bool) -> dict:
    try:
        outcomes = payload["results"]["validationOutcomesByProtocol"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError("fixture media report omits protocol outcomes") from exc
    for protocol in SERVICES:
        protocol_outcomes = outcomes.get(protocol)
        if (
            not isinstance(protocol_outcomes, dict)
            or sum(protocol_outcomes.values()) != 1
        ):
            raise RuntimeError(f"{protocol} media report must contain one outcome")
        succeeded = protocol_outcomes.get("success") == 1
        if healthy != succeeded:
            expected = "healthy" if healthy else "unreachable"
            raise RuntimeError(
                f"{protocol} sampled endpoint was not {expected}: {protocol_outcomes}"
            )
    return outcomes


class FixtureDomainFault:
    def __init__(self):
        self.project = os.environ.get(
            "VIDEOSIM_FIXTURE_STARTUP_PROJECT", "videosim-fixture-startup"
        )
        self.state_dir = Path(os.environ["VIDEOSIM_FIXTURE_STATE_DIR"]).resolve()
        startup_dir = Path(
            os.environ.get(
                "VIDEOSIM_FIXTURE_STARTUP_ARTIFACT_DIR",
                str(self.state_dir / "startup-validation"),
            )
        ).resolve()
        self.artifact_dir = Path(
            os.environ.get(
                "VIDEOSIM_FIXTURE_FAULT_ARTIFACT_DIR",
                str(self.state_dir / "fault-validation"),
            )
        ).resolve()
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.startup_result_path = startup_dir / "result.json"
        self.image = os.environ["VIDEOSIM_FIXTURE_IMAGE"]
        self.allow_mutable_image = (
            os.environ.get("VIDEOSIM_FIXTURE_STARTUP_ALLOW_MUTABLE_IMAGE") == "1"
        )
        self.dash_port = int(
            os.environ.get("VIDEOSIM_DASH_FIXTURE_HTTP_PORT", "18083")
        )
        self.hold_seconds = float(
            os.environ.get("VIDEOSIM_FIXTURE_FAULT_HOLD_SECONDS", "120")
        )
        self.stream_budget_seconds = float(
            os.environ.get("VIDEOSIM_FIXTURE_STARTUP_STREAM_BUDGET_SECONDS", "15")
        )
        for name, value, maximum in (
            ("VIDEOSIM_FIXTURE_FAULT_HOLD_SECONDS", self.hold_seconds, 3600),
            (
                "VIDEOSIM_FIXTURE_STARTUP_STREAM_BUDGET_SECONDS",
                self.stream_budget_seconds,
                120,
            ),
        ):
            if not math.isfinite(value) or not 0 <= value <= maximum:
                raise ValueError(f"{name} must be from 0 to {maximum}")
        if self.stream_budget_seconds == 0:
            raise ValueError(
                "VIDEOSIM_FIXTURE_STARTUP_STREAM_BUDGET_SECONDS must be greater than 0"
            )
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

    def dash_health_ready(self) -> bool:
        result = self.compose(
            "exec",
            "-T",
            "dash",
            "python",
            "-c",
            (
                "from urllib.request import urlopen; "
                f"raise SystemExit(urlopen('http://127.0.0.1:{self.dash_port}"
                "/healthz', timeout=2).read() != b'ok\\n')"
            ),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        return result.returncode == 0

    def capture_phase(self, phase: str, *, resources: bool):
        commands = {
            f"{phase}-compose-ps.txt": ("ps", "--all"),
            f"{phase}-docker.log": (
                "logs",
                "--no-color",
                "--timestamps",
                "srt",
                "dash",
            ),
        }
        if resources:
            commands[f"{phase}-docker-stats.json"] = (
                "stats",
                "--no-stream",
                "--format",
                "json",
            )
        for name, args in commands.items():
            path = self.artifact_dir / name
            with path.open("w", encoding="utf-8") as output:
                result = self.compose(
                    *args,
                    check=False,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    timeout=60,
                )
            if result.returncode or not path.read_text(encoding="utf-8").strip():
                raise RuntimeError(f"fixture {phase} artifact capture failed: {name}")

    def run_media(self, phase: str, *, healthy: bool) -> dict:
        result = self.compose(
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "python",
            "srt",
            "-m",
            "videosim",
            "worker-benchmark",
            "--scenario",
            "/artifacts/startup-scenario.json",
            "--iterations",
            "1",
            "--warmup-iterations",
            "0",
            "--max-concurrent-checks",
            "2",
            "--max-concurrent-deep-checks",
            "2",
            "--stream-budget-seconds",
            str(self.stream_budget_seconds),
            "--batch-budget-seconds",
            "0.001",
            "--require-full-validation-coverage",
            "--json",
            check=False,
            text=True,
            capture_output=True,
            timeout=180,
        )
        (self.artifact_dir / f"{phase}-media-validation.stderr").write_text(
            result.stderr, encoding="utf-8"
        )
        if result.returncode:
            raise RuntimeError(
                f"fixture {phase} media validator exited with {result.returncode}"
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"fixture {phase} media report is invalid JSON") from exc
        (self.artifact_dir / f"{phase}-media-validation.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        return validate_media_outcomes(payload, healthy=healthy)

    def validate_startup_evidence(self) -> dict:
        try:
            startup = json.loads(self.startup_result_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"fixture startup evidence is invalid: {exc}") from exc
        if startup.get("passed") is not True:
            raise RuntimeError("fixture startup evidence did not pass")
        if startup.get("project") != self.project:
            raise RuntimeError("fixture startup evidence project does not match")
        if startup.get("fixtureImage") != self.image:
            raise RuntimeError("fixture startup evidence image does not match")
        inventory = startup.get("fixtureInventory")
        if not isinstance(inventory, dict) or set(inventory) != SERVICES:
            raise RuntimeError("fixture startup evidence omits protocol inventory")
        scenario_path = self.state_dir / "startup-scenario.json"
        if not scenario_path.is_file():
            raise RuntimeError("fixture startup scenario is missing")
        return inventory

    def run(self) -> dict:
        result = {
            "schemaVersion": "videosim.fixture-domain-fault/v1",
            "passed": False,
            "capacityCertified": False,
            "allEndpointMediaValidated": False,
            "sampledEndpoints": 2,
            "startedAt": utc_now(),
            "project": self.project,
            "fixtureImage": self.image,
            "holdSeconds": self.hold_seconds,
            "checks": [],
            "errors": [],
        }
        try:
            if "@sha256:" not in self.image and not self.allow_mutable_image:
                raise RuntimeError(
                    "VIDEOSIM_FIXTURE_IMAGE must use an immutable @sha256 digest"
                )
            result["fixtureInventory"] = self.validate_startup_evidence()
            result["checks"].append("startup_evidence_validated")
            if self.running_services() != SERVICES:
                raise RuntimeError("fixture services must both be running before fault")
            result["baselineOutcomesByProtocol"] = self.run_media(
                "before", healthy=True
            )
            self.capture_phase("before", resources=True)
            result["checks"].append("baseline_media_api_process_resources_captured")

            fault_started = time.monotonic()
            result["faultRequestedAt"] = utc_now()
            self.compose("stop", "srt", "dash", timeout=180)
            self.wait_for(
                "fixture services to stop", lambda: not self.running_services()
            )
            result["servicesStoppedAt"] = utc_now()
            result["stopSeconds"] = round(time.monotonic() - fault_started, 3)
            if self.dash_health_ready():
                raise RuntimeError("DASH health API remained reachable during fault")
            result["faultOutcomesByProtocol"] = self.run_media(
                "fault", healthy=False
            )
            result["sampledMediaUnreachableAt"] = utc_now()
            result["sampledFaultDetectionSeconds"] = round(
                time.monotonic() - fault_started, 3
            )
            self.capture_phase("fault", resources=False)
            result["checks"].append("services_and_sampled_media_unreachable")

            time.sleep(self.hold_seconds)
            recovery_started = time.monotonic()
            result["recoveryRequestedAt"] = utc_now()
            self.compose("start", "srt", "dash", timeout=180)
            self.wait_for(
                "fixture services to run", lambda: self.running_services() == SERVICES
            )
            self.wait_for("DASH health API recovery", self.dash_health_ready)
            result["recoveryOutcomesByProtocol"] = self.run_media(
                "recovery", healthy=True
            )
            result["sampledMediaRecoveredAt"] = utc_now()
            result["sampledRecoverySeconds"] = round(
                time.monotonic() - recovery_started, 3
            )
            self.capture_phase("recovery", resources=True)
            recovery_log = (
                self.artifact_dir / "recovery-docker.log"
            ).read_text(encoding="utf-8")
            markers = [
                marker
                for marker in CRITICAL_LOG_MARKERS
                if marker in recovery_log.lower()
            ]
            if markers:
                raise RuntimeError(
                    "fixture recovery logs contain critical markers: "
                    + ", ".join(markers)
                )
            result["checks"].append("sampled_media_api_process_resources_recovered")
            result["checks"].append("docker_logs_clean")
            result["passed"] = True
        except Exception as exc:
            result["errors"].append(str(exc))
        finally:
            if self.running_services() != SERVICES:
                recovery = self.compose(
                    "start", "srt", "dash", check=False, timeout=180
                )
                result["failSafeRecoveryAttempted"] = True
                if recovery.returncode:
                    result["errors"].append("fail-safe fixture recovery failed")
                else:
                    try:
                        self.wait_for(
                            "fail-safe fixture recovery",
                            lambda: self.running_services() == SERVICES,
                        )
                    except Exception as exc:
                        result["errors"].append(str(exc))
            result["endedAt"] = utc_now()
            result["servicesRunningAtEnd"] = self.running_services() == SERVICES
            if not result["servicesRunningAtEnd"]:
                result["passed"] = False
            path = self.artifact_dir / "result.json"
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(result, indent=2, sort_keys=True), encoding="utf-8"
            )
            temporary.replace(path)
        return result


def main() -> int:
    result = FixtureDomainFault().run()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
