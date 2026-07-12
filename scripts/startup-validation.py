#!/usr/bin/env python3
"""Boot the Compose stack and validate a real feed through its HTTP API."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]


class StartupValidation:
    def __init__(self):
        self.project = os.environ.get(
            "VIDEOSIM_STARTUP_PROJECT", "videosim-startup-validation"
        )
        self.http_port = int(os.environ.get("VIDEOSIM_STARTUP_HTTP_PORT", "18080"))
        self.feed_port = int(os.environ.get("VIDEOSIM_STARTUP_FEED_PORT", "19000"))
        self.keep = os.environ.get("VIDEOSIM_STARTUP_KEEP") == "1"
        self.no_build = os.environ.get("VIDEOSIM_STARTUP_NO_BUILD") == "1"
        self.artifact_dir = Path(
            os.environ.get(
                "VIDEOSIM_STARTUP_ARTIFACT_DIR",
                str(ROOT / "artifacts" / "startup-validation"),
            )
        ).resolve()
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.compose_env = os.environ.copy()
        self.compose_env.update(
            {
                "VIDEOSIM_HTTP_PORT": str(self.http_port),
                "VIDEOSIM_FEED_PORT": str(self.feed_port),
                "VIDEOSIM_FEED_PORT_RANGE": f"{self.feed_port + 1}-{self.feed_port + 10}",
            }
        )
        self.base_url = f"http://127.0.0.1:{self.http_port}"

    def compose(self, *args: str, check: bool = True, timeout: int = 600):
        return subprocess.run(
            ["docker", "compose", "-p", self.project, *args],
            cwd=ROOT,
            env=self.compose_env,
            check=check,
            timeout=timeout,
        )

    def capture_artifacts(self):
        commands = {
            "compose-ps.txt": ("ps", "--all"),
            "docker.log": ("logs", "--no-color", "--timestamps", "app", "worker"),
        }
        for name, args in commands.items():
            with (self.artifact_dir / name).open("w", encoding="utf-8") as output:
                subprocess.run(
                    ["docker", "compose", "-p", self.project, *args],
                    cwd=ROOT,
                    env=self.compose_env,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    check=False,
                    timeout=60,
                )

    def get_json(self, path: str, timeout: float = 5) -> dict:
        with urlopen(f"{self.base_url}{path}", timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def post_form(self, path: str, fields: dict[str, str], timeout: float = 70):
        request = Request(
            f"{self.base_url}{path}",
            data=urlencode(fields).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urlopen(request, timeout=timeout) as response:
            response.read()

    def wait_for(self, description: str, condition, timeout: float = 90):
        deadline = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < deadline:
            try:
                value = condition()
                if value:
                    return value
            except (HTTPError, URLError, OSError, json.JSONDecodeError) as exc:
                last_error = exc
            time.sleep(1)
        detail = f": {last_error}" if last_error else ""
        raise RuntimeError(f"timed out waiting for {description}{detail}")

    def state(self) -> dict:
        return self.get_json("/state.json")

    def stream(self, stream_id: str) -> dict:
        return next(
            stream for stream in self.state()["streams"] if stream["id"] == stream_id
        )

    def run(self) -> dict:
        result = {
            "passed": False,
            "startedAt": datetime.now(timezone.utc).isoformat(),
            "project": self.project,
            "baseUrl": self.base_url,
            "checks": [],
        }
        try:
            self.compose("version", timeout=30)
            self.compose("config", "--quiet", timeout=30)
            self.compose("down", "--volumes", "--remove-orphans", check=False, timeout=60)
            if not self.no_build:
                self.compose("build", "app", "worker")
            self.compose("up", "-d", "--no-build", "app")
            self.wait_for(
                "GUI readiness",
                lambda: self.get_json("/readyz").get("status") == "ready",
            )
            result["checks"].append("api_ready")

            self.compose("up", "-d", "--no-build", "worker")
            self.wait_for(
                "worker registration",
                lambda: any(
                    worker.get("id") == "worker-1"
                    for worker in self.state()["monitor"].get("workers", [])
                ),
            )
            result["checks"].append("worker_registered")

            self.post_form(
                "/streams/create",
                {
                    "name": "Startup validation feed",
                    "protocol": "srt",
                    "mode": "normal",
                    "source": "generated",
                    "framerate": "10",
                },
            )
            created = next(
                stream
                for stream in self.state()["streams"]
                if stream["name"] == "Startup validation feed"
            )
            stream_id = created["id"]
            result["streamId"] = stream_id
            result["endpoint"] = created["endpoint"]
            result["checks"].append("feed_created")

            self.post_form(
                "/start",
                {"stream_id": stream_id, "protocol": "srt", "mode": "normal"},
            )
            self.wait_for(
                "running feed", lambda: self.stream(stream_id)["status"] == "running"
            )
            result["checks"].append("feed_running")
            time.sleep(4)

            self.post_form("/validate", {"stream_id": stream_id})
            validated = self.stream(stream_id)
            expected_lines = (
                "Validation PASS",
                "reachable=True",
                "video_present=True",
                "audio_present=True",
                "captions_present=True",
            )
            missing = [
                line for line in expected_lines if line not in validated["validationOutput"]
            ]
            if missing:
                raise RuntimeError(
                    "normal feed validation did not prove expected state: "
                    + ", ".join(missing)
                )
            result["validationOutput"] = validated["validationOutput"]
            result["checks"].append("normal_feed_validated")

            self.post_form("/stop", {"stream_id": stream_id})
            self.wait_for(
                "stopped feed", lambda: self.stream(stream_id)["status"] == "stopped"
            )
            result["checks"].append("feed_stopped")
            result["passed"] = True
            return result
        except Exception as exc:
            result["error"] = str(exc)
            raise
        finally:
            result["finishedAt"] = datetime.now(timezone.utc).isoformat()
            (self.artifact_dir / "result.json").write_text(
                json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
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
    validation = StartupValidation()
    try:
        result = validation.run()
    except Exception as exc:
        print(f"Startup validation FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    print(f"Artifacts: {validation.artifact_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
