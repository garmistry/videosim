#!/usr/bin/env python3
"""Boot two APIs plus the generated runtime and validate real SRT handoff."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.generated-runtime-validation.yml"


class GeneratedRuntimeValidation:
    def __init__(self):
        self.project = os.environ.get(
            "VIDEOSIM_GENERATED_VALIDATION_PROJECT",
            "videosim-generated-runtime-validation",
        )
        self.api_a_port = int(
            os.environ.get("VIDEOSIM_GENERATED_VALIDATION_API_A_PORT", "18081")
        )
        self.api_b_port = int(
            os.environ.get("VIDEOSIM_GENERATED_VALIDATION_API_B_PORT", "18082")
        )
        self.feed_port = int(
            os.environ.get("VIDEOSIM_GENERATED_VALIDATION_FEED_PORT", "19100")
        )
        self.keep = os.environ.get("VIDEOSIM_GENERATED_VALIDATION_KEEP") == "1"
        self.no_build = (
            os.environ.get("VIDEOSIM_GENERATED_VALIDATION_NO_BUILD") == "1"
        )
        self.artifact_dir = Path(
            os.environ.get(
                "VIDEOSIM_GENERATED_VALIDATION_ARTIFACT_DIR",
                str(ROOT / "artifacts/generated-runtime-validation"),
            )
        ).resolve()
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.environment = os.environ | {
            "VIDEOSIM_GENERATED_VALIDATION_API_A_PORT": str(self.api_a_port),
            "VIDEOSIM_GENERATED_VALIDATION_API_B_PORT": str(self.api_b_port),
            "VIDEOSIM_GENERATED_VALIDATION_FEED_PORT": str(self.feed_port),
        }
        self.api_a = f"http://127.0.0.1:{self.api_a_port}"
        self.api_b = f"http://127.0.0.1:{self.api_b_port}"

    def compose(
        self,
        *args: str,
        check: bool = True,
        timeout: int = 600,
        capture: bool = False,
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                self.project,
                "-f",
                str(COMPOSE),
                *args,
            ],
            cwd=ROOT,
            env=self.environment,
            text=True,
            capture_output=capture,
            check=check,
            timeout=timeout,
        )

    def get_json(self, base_url: str, path: str) -> dict:
        with urlopen(f"{base_url}{path}", timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def post_form(self, base_url: str, path: str, fields: dict):
        request = Request(
            f"{base_url}{path}",
            data=urlencode(fields).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urlopen(request, timeout=20) as response:
            response.read()

    def wait_for(self, description: str, condition, timeout: float = 90):
        deadline = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < deadline:
            try:
                value = condition()
                if value:
                    return value
            except (HTTPError, URLError, OSError, ValueError) as exc:
                last_error = exc
            time.sleep(0.5)
        detail = f": {last_error}" if last_error else ""
        raise RuntimeError(f"timed out waiting for {description}{detail}")

    def detail(self, base_url: str, feed_id: str) -> dict:
        return self.get_json(base_url, f"/api/operator/feeds/{feed_id}")

    def runtime_log(self) -> str:
        return self.compose(
            "logs", "--no-color", "generated-feed-runtime", capture=True, timeout=30
        ).stdout

    def validate_media(self, name: str, *, expect_pass: bool) -> str:
        result = self.compose(
            "run",
            "--rm",
            "validator",
            "python",
            "-m",
            "videosim",
            "validate",
            "--profile",
            "profiles/srt-normal.yaml",
            "--endpoint",
            f"srt://generated-feed-runtime:{self.feed_port}?mode=caller",
            "--framerate",
            "10",
            check=False,
            timeout=90,
            capture=True,
        )
        output = result.stdout + result.stderr
        (self.artifact_dir / name).write_text(output, encoding="utf-8")
        if (result.returncode == 0) != expect_pass:
            raise RuntimeError(
                f"media validation {'passed' if result.returncode == 0 else 'failed'} "
                f"unexpectedly: {output[-1000:]}"
            )
        if expect_pass:
            for expected in (
                "Validation PASS",
                "reachable=True",
                "video_present=True",
                "audio_present=True",
                "captions_present=True",
            ):
                if expected not in output:
                    raise RuntimeError(f"media validation omitted {expected}")
        return output

    def capture_artifacts(self):
        for name, args in {
            "compose-ps.txt": ("ps", "--all"),
            "docker.log": (
                "logs",
                "--no-color",
                "--timestamps",
                "postgres",
                "api-a",
                "api-b",
                "generated-feed-runtime",
            ),
        }.items():
            with (self.artifact_dir / name).open("w", encoding="utf-8") as output:
                subprocess.run(
                    [
                        "docker",
                        "compose",
                        "-p",
                        self.project,
                        "-f",
                        str(COMPOSE),
                        *args,
                    ],
                    cwd=ROOT,
                    env=self.environment,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    check=False,
                    timeout=60,
                )

    def run(self) -> dict:
        result = {
            "passed": False,
            "startedAt": datetime.now(timezone.utc).isoformat(),
            "checks": [],
            "apiA": self.api_a,
            "apiB": self.api_b,
        }
        try:
            self.compose("version", timeout=30)
            self.compose("config", "--quiet", timeout=30)
            self.compose(
                "down", "--volumes", "--remove-orphans", check=False, timeout=60
            )
            if not self.no_build:
                self.compose("build", "api-a")
            self.compose(
                "up",
                "-d",
                "--no-build",
                "api-a",
                "api-b",
                "generated-feed-runtime",
            )
            for name, url in (("api-a", self.api_a), ("api-b", self.api_b)):
                self.wait_for(
                    f"{name} readiness",
                    lambda url=url: self.get_json(url, "/readyz").get("status")
                    == "ready",
                )
            result["checks"].append("two_apis_ready")

            if self.get_json(self.api_b, "/state.json")["streams"]:
                raise RuntimeError("replica B did not start with an empty process cache")
            self.post_form(
                self.api_a,
                "/streams/create",
                {
                    "name": "Generated runtime validation",
                    "source": "generated",
                    "protocol": "srt",
                    "mode": "normal",
                    "framerate": "10",
                },
            )
            catalog = self.get_json(self.api_b, "/api/operator/feeds?limit=100")
            feed = next(
                item
                for item in catalog["feeds"]
                if item["name"] == "Generated runtime validation"
            )
            feed_id = feed["id"]
            result["feedId"] = feed_id
            result["endpoint"] = feed["endpoint"]
            detail = self.detail(self.api_b, feed_id)
            stream = detail["streams"][0]
            if detail["operatorReadOnly"] or stream["runtimeKnown"] is not False:
                raise RuntimeError("replica B did not expose writable config-only state")
            result["checks"].append("replica_create_visible")

            self.post_form(
                self.api_b,
                "/start",
                {
                    "stream_id": feed_id,
                    "config_version": stream["configVersion"],
                    "protocol": "srt",
                    "mode": "normal",
                },
            )
            started = self.wait_for(
                "durable running intent",
                lambda: (
                    candidate
                    if (
                        candidate := self.detail(self.api_a, feed_id)["streams"][0]
                    )["desiredState"]
                    == "running"
                    else None
                ),
            )
            self.wait_for(
                "runtime process start",
                lambda: feed_id in self.runtime_log()
                and "started" in self.runtime_log(),
            )
            result["checks"].append("remote_start_reconciled")
            time.sleep(4)
            result["validation"] = self.validate_media(
                "validation-before-restart.txt", expect_pass=True
            )
            result["checks"].append("media_validated_before_restart")

            (self.artifact_dir / "runtime-before-restart.log").write_text(
                self.runtime_log(), encoding="utf-8"
            )
            restart_started = time.monotonic()
            self.compose("kill", "generated-feed-runtime", timeout=30)
            self.compose("rm", "-f", "generated-feed-runtime", timeout=30)
            self.compose("up", "-d", "--no-build", "generated-feed-runtime")
            self.wait_for(
                "runtime restart ownership",
                lambda: feed_id in self.runtime_log()
                and "started" in self.runtime_log(),
            )
            result["runtimeRecoverySeconds"] = round(
                time.monotonic() - restart_started, 3
            )
            time.sleep(4)
            self.validate_media("validation-after-restart.txt", expect_pass=True)
            result["checks"].append("media_validated_after_restart")

            current = self.detail(self.api_a, feed_id)["streams"][0]
            self.post_form(
                self.api_a,
                "/stop",
                {
                    "stream_id": feed_id,
                    "config_version": current["configVersion"],
                },
            )
            self.wait_for(
                "durable stopped intent",
                lambda: self.detail(self.api_b, feed_id)["streams"][0][
                    "desiredState"
                ]
                == "stopped",
            )
            self.wait_for(
                "runtime process stop",
                lambda: feed_id in self.runtime_log()
                and "stopped" in self.runtime_log(),
            )
            self.validate_media("validation-after-stop.txt", expect_pass=False)
            result["checks"].append("remote_stop_unreachable")
            result["passed"] = True
            return result
        except Exception as exc:
            result["error"] = str(exc)
            raise
        finally:
            result["finishedAt"] = datetime.now(timezone.utc).isoformat()
            (self.artifact_dir / "result.json").write_text(
                json.dumps(result, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            try:
                self.capture_artifacts()
            finally:
                if not self.keep:
                    self.compose(
                        "down",
                        "--volumes",
                        "--remove-orphans",
                        check=False,
                        timeout=90,
                    )


def main() -> int:
    validation = GeneratedRuntimeValidation()
    try:
        result = validation.run()
    except Exception as exc:
        print(f"Generated runtime validation FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    print(f"Artifacts: {validation.artifact_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
