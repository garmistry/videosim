#!/usr/bin/env python3
"""Boot and validate one distributed fixture domain."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "docker-compose.fixture-domain.yml"
CRITICAL_LOG_MARKERS = (
    "traceback (most recent call last)",
    "fixture process exited",
)


class FixtureDomainStartup:
    def __init__(self):
        self.project = os.environ.get(
            "VIDEOSIM_FIXTURE_STARTUP_PROJECT", "videosim-fixture-startup"
        )
        self.state_dir = Path(
            os.environ["VIDEOSIM_FIXTURE_STATE_DIR"]
        ).resolve()
        self.artifact_dir = Path(
            os.environ.get(
                "VIDEOSIM_FIXTURE_STARTUP_ARTIFACT_DIR",
                str(self.state_dir / "startup-validation"),
            )
        ).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.keep = os.environ.get("VIDEOSIM_FIXTURE_STARTUP_KEEP") == "1"
        self.no_pull = os.environ.get("VIDEOSIM_FIXTURE_STARTUP_NO_PULL") == "1"
        self.allow_mutable_image = (
            os.environ.get("VIDEOSIM_FIXTURE_STARTUP_ALLOW_MUTABLE_IMAGE") == "1"
        )
        self.image = os.environ["VIDEOSIM_FIXTURE_IMAGE"]
        self.dash_port = int(
            os.environ.get("VIDEOSIM_DASH_FIXTURE_HTTP_PORT", "18083")
        )
        self.stream_budget_seconds = float(
            os.environ.get("VIDEOSIM_FIXTURE_STARTUP_STREAM_BUDGET_SECONDS", "15")
        )
        self.compose_env = os.environ.copy()
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
            env=self.compose_env,
            check=check,
            timeout=options.pop("timeout", 600),
            **options,
        )

    def wait_for(self, description: str, condition, timeout: float = 120):
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

    def capture_artifacts(self):
        commands = {
            "compose-ps.txt": ("ps", "--all"),
            "docker.log": ("logs", "--no-color", "--timestamps", "srt", "dash"),
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

    def dash_health_api_ready(self) -> bool:
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

    def create_startup_scenario(self) -> Path:
        streams = []
        for protocol in ("srt", "dash"):
            state_path = self.state_dir / f"{protocol}-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            healthy = next(
                stream
                for stream in state["streams"]
                if stream.get("fixtureBehavior") == "healthy"
            )
            healthy = dict(healthy)
            healthy["id"] = f"startup-{protocol}"
            healthy["name"] = f"Startup {protocol.upper()} fixture"
            streams.append(healthy)
        path = self.state_dir / "startup-scenario.json"
        path.write_text(
            json.dumps({"streams": streams}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return path

    def validate_media(self) -> dict:
        self.create_startup_scenario()
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
            timeout=120,
        )
        (self.artifact_dir / "media-validation.stderr").write_text(
            result.stderr, encoding="utf-8"
        )
        if result.returncode:
            raise RuntimeError(
                "fixture startup media validator failed with "
                f"exit status {result.returncode}"
            )
        payload = json.loads(result.stdout)
        (self.artifact_dir / "media-validation.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        outcomes = payload["results"]["validationOutcomesByProtocol"]
        for protocol in ("srt", "dash"):
            if outcomes.get(protocol) != {"success": 1}:
                raise RuntimeError(
                    f"{protocol} startup media validation did not pass: "
                    f"{outcomes.get(protocol)}"
                )
        if not payload.get("passed"):
            raise RuntimeError("fixture startup media benchmark failed")
        return payload

    def run(self) -> dict:
        result = {
            "passed": False,
            "startedAt": datetime.now(timezone.utc).isoformat(),
            "project": self.project,
            "fixtureImage": self.image,
            "immutableImage": "@sha256:" in self.image,
            "checks": [],
            "errors": [],
        }
        captured = False
        try:
            if not result["immutableImage"] and not self.allow_mutable_image:
                raise RuntimeError(
                    "VIDEOSIM_FIXTURE_IMAGE must use an immutable @sha256 digest"
                )
            self.compose("version", timeout=30)
            engine = subprocess.run(
                ["docker", "info", "--format", "{{.OSType}}"],
                text=True,
                capture_output=True,
                check=True,
                timeout=30,
            ).stdout.strip()
            if engine != "linux":
                raise RuntimeError("fixture domains require a Linux Docker engine")
            result["checks"].append("linux_docker_engine")

            for name in (
                "srt-state.json",
                "dash-state.json",
                "startup-scenario.json",
            ):
                (self.state_dir / name).unlink(missing_ok=True)
            self.compose("config", "--quiet", timeout=30)
            result["checks"].append("compose_rendered")
            self.compose(
                "down", "--remove-orphans", check=False, timeout=120
            )
            if not self.no_pull:
                self.compose("pull", "srt", "dash")
                result["checks"].append("immutable_image_pulled")
            self.compose("up", "-d", "--no-build", "srt", "dash")
            self.wait_for(
                "fixture services",
                lambda: self.running_services() == {"srt", "dash"},
            )
            self.wait_for(
                "fixture state files",
                lambda: all(
                    (self.state_dir / f"{protocol}-state.json").is_file()
                    for protocol in ("srt", "dash")
                ),
            )
            result["checks"].append("fixture_services_running")

            self.wait_for(
                "DASH health API",
                self.dash_health_api_ready,
            )
            result["checks"].append("dash_health_api")
            media = self.validate_media()
            result["validationOutcomesByProtocol"] = media["results"][
                "validationOutcomesByProtocol"
            ]
            result["checks"].append("srt_dash_media_validated")

            self.capture_artifacts()
            captured = True
            log_output = (self.artifact_dir / "docker.log").read_text(
                encoding="utf-8"
            )
            if not log_output.strip():
                raise RuntimeError("fixture Docker logs are empty")
            lowered = log_output.lower()
            found = [marker for marker in CRITICAL_LOG_MARKERS if marker in lowered]
            if found:
                raise RuntimeError(
                    "fixture Docker logs contain critical markers: "
                    + ", ".join(found)
                )
            result["checks"].append("docker_logs_clean")
            result["passed"] = True
        except Exception as exc:
            result["errors"].append(str(exc))
        finally:
            if not captured:
                try:
                    self.capture_artifacts()
                except Exception as exc:
                    result["errors"].append(f"artifact capture failed: {exc}")
            if not self.keep:
                try:
                    self.compose(
                        "down", "--remove-orphans", check=False, timeout=120
                    )
                except Exception as exc:
                    result["errors"].append(f"cleanup failed: {exc}")
                    result["passed"] = False
            result["endedAt"] = datetime.now(timezone.utc).isoformat()
            (self.artifact_dir / "result.json").write_text(
                json.dumps(result, indent=2, sort_keys=True), encoding="utf-8"
            )
        return result


def main() -> int:
    result = FixtureDomainStartup().run()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
