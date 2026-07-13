#!/usr/bin/env python3
"""Boot and validate one local durable fixture/worker domain."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_COMPOSE = ROOT / "docker-compose.fixture-domain.yml"
VALIDATION_MONITORS = (
    "feed_reachable",
    "essence_video_present",
    "essence_audio_present",
    "essence_captions_present",
)
CRITICAL_LOG_MARKERS = (
    "traceback (most recent call last)",
    "fixture process exited",
)


def expected_scenario_counts(scenario: dict) -> dict[str, int]:
    return dict(
        Counter(
            f"{stream['protocol']}:{stream['fixtureBehavior']}"
            for stream in scenario["streams"]
        )
    )


def validate_durable_summary(
    summary: dict, expected_counts: dict[str, int], worker_count: int
) -> list[str]:
    errors = []
    expected_streams = sum(expected_counts.values())
    expected_min = expected_streams // worker_count
    expected_max = (expected_streams + worker_count - 1) // worker_count
    exact_fields = {
        "freshWorkers": worker_count,
        "activeLeases": expected_streams,
        "minimumLeases": expected_min,
        "maximumLeases": expected_max,
        "validatedStreams": expected_streams,
        "blackFrozenAlarms": 0,
    }
    for field, expected in exact_fields.items():
        if summary.get(field) != expected:
            errors.append(f"{field}={summary.get(field)} expected {expected}")

    expected_outcomes = {}
    for key, count in expected_counts.items():
        protocol, behavior = key.split(":", 1)
        if behavior == "healthy":
            status = "healthy"
        elif protocol == "dash" and behavior == "malformed":
            status = "unhealthy"
        else:
            status = "timeout"
        expected_outcomes[f"{key}:{status}"] = count
    if summary.get("outcomes") != expected_outcomes:
        errors.append(
            f"outcomes={summary.get('outcomes')} expected {expected_outcomes}"
        )

    malformed_dash = expected_counts.get("dash:malformed", 0)
    expected_alarms = {
        f"dash:malformed:{monitor_id}": malformed_dash
        for monitor_id in VALIDATION_MONITORS
        if malformed_dash
    }
    if summary.get("activeAlarms") != expected_alarms:
        errors.append(
            f"activeAlarms={summary.get('activeAlarms')} expected {expected_alarms}"
        )
    if summary.get("activeAlarmCount") != malformed_dash * len(
        VALIDATION_MONITORS
    ):
        errors.append(
            "activeAlarmCount="
            f"{summary.get('activeAlarmCount')} expected "
            f"{malformed_dash * len(VALIDATION_MONITORS)}"
        )
    return errors


class DurableFixtureStartup:
    def __init__(self):
        self.project = os.environ.get(
            "VIDEOSIM_DURABLE_FIXTURE_PROJECT", "videosim-durable-fixture-startup"
        )
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", self.project):
            raise ValueError("VIDEOSIM_DURABLE_FIXTURE_PROJECT is invalid")
        self.image = os.environ["VIDEOSIM_DURABLE_FIXTURE_IMAGE"]
        self.postgres_image = os.environ.get(
            "VIDEOSIM_DURABLE_FIXTURE_POSTGRES_IMAGE", "postgres:17-alpine"
        )
        self.allow_mutable_image = (
            os.environ.get("VIDEOSIM_DURABLE_FIXTURE_ALLOW_MUTABLE_IMAGE") == "1"
        )
        self.no_pull = (
            os.environ.get("VIDEOSIM_DURABLE_FIXTURE_NO_PULL") == "1"
        )
        self.keep = os.environ.get("VIDEOSIM_DURABLE_FIXTURE_KEEP") == "1"
        self.worker_count = int(
            os.environ.get("VIDEOSIM_DURABLE_FIXTURE_WORKER_COUNT", "11")
        )
        self.max_concurrent_checks = int(
            os.environ.get("VIDEOSIM_DURABLE_FIXTURE_MAX_CONCURRENT_CHECKS", "12")
        )
        self.timeout_seconds = float(
            os.environ.get("VIDEOSIM_DURABLE_FIXTURE_TIMEOUT_SECONDS", "600")
        )
        self.postgres_port = int(
            os.environ.get("VIDEOSIM_DURABLE_FIXTURE_POSTGRES_PORT", "55440")
        )
        self.http_port = int(
            os.environ.get("VIDEOSIM_DURABLE_FIXTURE_HTTP_PORT", "18085")
        )
        self.feed_port = int(
            os.environ.get("VIDEOSIM_DURABLE_FIXTURE_FEED_PORT", "29950")
        )
        if self.worker_count < 1 or self.max_concurrent_checks < 1:
            raise ValueError("worker and concurrency counts must be positive")
        self.artifact_dir = Path(
            os.environ.get(
                "VIDEOSIM_DURABLE_FIXTURE_ARTIFACT_DIR",
                str(ROOT / "artifacts" / "durable-fixture-startup"),
            )
        ).resolve()
        self.state_dir = self.artifact_dir / "fixture-state"
        self.fixture_artifact_dir = self.artifact_dir / "fixture-startup"
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.scenario_manifest = Path(
            os.environ.get(
                "VIDEOSIM_DURABLE_FIXTURE_SCENARIO_MANIFEST",
                str(ROOT / "scale" / "fixtures" / "mixed-440-domain.json"),
            )
        ).resolve()
        manifest = json.loads(self.scenario_manifest.read_text(encoding="utf-8"))
        self.expected_streams = int(manifest["streamCount"])
        self.scenario_path = self.artifact_dir / "mixed-domain.json"
        self.import_path = self.artifact_dir / "catalog-import.json"
        self.postgres_name = f"{self.project}-postgres"
        self.app_name = f"{self.project}-app"
        self.worker_names = [
            f"{self.project}-worker-{index:02d}"
            for index in range(1, self.worker_count + 1)
        ]
        self.worker_ids = [
            f"{self.project}-worker-{index:02d}"
            for index in range(1, self.worker_count + 1)
        ]
        self.database_url = (
            "postgresql://videosim:videosim_test@127.0.0.1:"
            f"{self.postgres_port}/videosim"
        )
        self.fixture_project = f"{self.project}-fixtures"
        self.fixture_env = os.environ.copy()
        self.fixture_env.update(
            {
                "VIDEOSIM_FIXTURE_IMAGE": self.image,
                "VIDEOSIM_FIXTURE_ADVERTISED_HOST": "127.0.0.1",
                "VIDEOSIM_FIXTURE_STATE_DIR": str(self.state_dir),
                "VIDEOSIM_FIXTURE_STARTUP_ARTIFACT_DIR": str(
                    self.fixture_artifact_dir
                ),
                "VIDEOSIM_FIXTURE_STARTUP_PROJECT": self.fixture_project,
                "VIDEOSIM_FIXTURE_STARTUP_KEEP": "1",
                "VIDEOSIM_FIXTURE_STARTUP_NO_PULL": "1" if self.no_pull else "0",
                "VIDEOSIM_FIXTURE_STARTUP_ALLOW_MUTABLE_IMAGE": (
                    "1" if self.allow_mutable_image else "0"
                ),
            }
        )

    def run_command(self, args: list[str], **options):
        return subprocess.run(
            args,
            cwd=ROOT,
            text=options.pop("text", True),
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

    def compose_fixture(self, *args: str, **options):
        return subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                self.fixture_project,
                "-f",
                str(FIXTURE_COMPOSE),
                *args,
            ],
            cwd=ROOT,
            env=self.fixture_env,
            text=options.pop("text", True),
            timeout=options.pop("timeout", 180),
            **options,
        )

    def image_command(self, *args: str, volumes: list[str] | None = None):
        command = ["docker", "run", "--rm", "--network", "host"]
        for volume in volumes or []:
            command.extend(("-v", volume))
        command.extend((self.image, *args))
        return self.run_command(command, check=True)

    def remove_containers(self):
        self.run_command(
            [
                "docker",
                "rm",
                "-f",
                self.app_name,
                self.postgres_name,
                *self.worker_names,
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )

    def cleanup(self):
        self.remove_containers()
        self.compose_fixture(
            "down",
            "--remove-orphans",
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def start_fixture(self):
        result = subprocess.run(
            [sys.executable, "scripts/fixture-domain-startup.py"],
            cwd=ROOT,
            env=self.fixture_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=600,
        )
        (self.artifact_dir / "fixture-startup.log").write_text(
            result.stdout, encoding="utf-8"
        )
        if result.returncode:
            raise RuntimeError(
                f"fixture startup failed with exit status {result.returncode}"
            )

    def start_postgres(self):
        self.run_command(
            [
                "docker",
                "run",
                "-d",
                "--name",
                self.postgres_name,
                "-e",
                "POSTGRES_DB=videosim",
                "-e",
                "POSTGRES_USER=videosim",
                "-e",
                "POSTGRES_PASSWORD=videosim_test",
                "-p",
                f"127.0.0.1:{self.postgres_port}:5432",
                self.postgres_image,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        self.wait_for(
            "PostgreSQL",
            lambda: self.run_command(
                [
                    "docker",
                    "exec",
                    self.postgres_name,
                    "pg_isready",
                    "-U",
                    "videosim",
                    "-d",
                    "videosim",
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).returncode
            == 0,
        )

    def migrate_and_import(self):
        self.image_command(
            "python3",
            "-m",
            "videosim",
            "migrate",
            "--database-url",
            self.database_url,
        )
        self.image_command(
            "python3",
            "-m",
            "videosim",
            "fixture-scenario",
            "--manifest",
            "/input/scenario.json",
            "--srt-state",
            "/state/srt-state.json",
            "--dash-state",
            "/state/dash-state.json",
            "--state-path",
            "/artifacts/mixed-domain.json",
            volumes=[
                f"{self.scenario_manifest}:/input/scenario.json:ro",
                f"{self.state_dir}:/state:ro",
                f"{self.artifact_dir}:/artifacts",
            ],
        )
        self.image_command(
            "python3",
            "-m",
            "videosim",
            "import-fixture-scenario",
            "--database-url",
            self.database_url,
            "--state",
            "/artifacts/mixed-domain.json",
            "--output",
            "/artifacts/catalog-import.json",
            volumes=[f"{self.artifact_dir}:/artifacts"],
        )

    def container_json(self, path: str) -> dict:
        url = f"http://127.0.0.1:{self.http_port}{path}"
        result = self.run_command(
            [
                "docker",
                "exec",
                self.app_name,
                "python3",
                "-c",
                (
                    "import sys; from urllib.request import urlopen; "
                    "sys.stdout.write(urlopen(sys.argv[1], timeout=5).read().decode())"
                ),
                url,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        return json.loads(result.stdout)

    def start_app(self):
        self.run_command(
            [
                "docker",
                "run",
                "-d",
                "--name",
                self.app_name,
                "--network",
                "host",
                "-e",
                f"VIDEOSIM_DATABASE_URL={self.database_url}",
                "-e",
                "VIDEOSIM_WORKER_FRESHNESS_SECONDS=30",
                "-e",
                "VIDEOSIM_VERBOSE=1",
                self.image,
                "python3",
                "-m",
                "videosim",
                "gui",
                "--host",
                "0.0.0.0",
                "--http-port",
                str(self.http_port),
                "--feed-port",
                str(self.feed_port),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        self.wait_for(
            "durable API",
            lambda: self.container_json("/readyz").get("status") == "ready",
        )

    def start_workers(self) -> str:
        started_at = datetime.now(timezone.utc).isoformat()
        for name, worker_id in zip(self.worker_names, self.worker_ids):
            self.run_command(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    name,
                    "--network",
                    "host",
                    self.image,
                    "python3",
                    "-m",
                    "videosim",
                    "worker",
                    "--control-plane-url",
                    f"http://127.0.0.1:{self.http_port}",
                    "--worker-id",
                    worker_id,
                    "--srt-host",
                    "127.0.0.1",
                    "--poll-interval-seconds",
                    "5",
                    "--repeat-interval-seconds",
                    "5",
                    "--heartbeat-interval-seconds",
                    "5",
                    "--max-streams",
                    "60",
                    "--max-srt-streams",
                    "30",
                    "--max-dash-streams",
                    "30",
                    "--max-concurrent-checks",
                    str(self.max_concurrent_checks),
                    "--max-concurrent-deep-checks",
                    "2",
                    "--stream-budget-seconds",
                    "15",
                    "--deep-check-interval-seconds",
                    "3600",
                    "--batch-budget-seconds",
                    "0.001",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
        return started_at

    def psql(self, query: str) -> str:
        result = self.run_command(
            [
                "docker",
                "exec",
                self.postgres_name,
                "psql",
                "-U",
                "videosim",
                "-d",
                "videosim",
                "-At",
                "-F",
                "\t",
                "-c",
                query,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        return result.stdout.strip()

    def durable_summary(self, started_at: str) -> dict:
        prefix = f"{self.project}-worker-%"
        counts = self.psql(
            f"""
            WITH lease_counts AS (
              SELECT worker_id, count(*) AS n
              FROM leases
              WHERE state='active' AND worker_id LIKE '{prefix}'
              GROUP BY worker_id
            )
            SELECT
              (SELECT count(*) FROM workers WHERE worker_id LIKE '{prefix}'
                AND last_heartbeat_at >= now() - interval '30 seconds'),
              (SELECT count(*) FROM leases WHERE state='active'
                AND worker_id LIKE '{prefix}'),
              COALESCE((SELECT min(n) FROM lease_counts), 0),
              COALESCE((SELECT max(n) FROM lease_counts), 0),
              (SELECT count(DISTINCT stream_id) FROM check_results
                WHERE check_id='probe.validation' AND status<>'skipped'
                AND created_at >= timestamptz '{started_at}'),
              (SELECT count(*) FROM current_alarms WHERE active),
              (SELECT count(*) FROM current_alarms WHERE active
                AND monitor_id IN ('black_video_detected','frozen_video_detected'));
            """
        ).split("\t")
        summary = {
            "freshWorkers": int(counts[0]),
            "activeLeases": int(counts[1]),
            "minimumLeases": int(counts[2]),
            "maximumLeases": int(counts[3]),
            "validatedStreams": int(counts[4]),
            "activeAlarmCount": int(counts[5]),
            "blackFrozenAlarms": int(counts[6]),
        }
        outcomes = self.psql(
            f"""
            WITH latest AS (
              SELECT DISTINCT ON (stream_id) stream_id, status
              FROM check_results
              WHERE check_id='probe.validation' AND status<>'skipped'
                AND created_at >= timestamptz '{started_at}'
              ORDER BY stream_id, observed_at DESC, created_at DESC
            )
            SELECT
              split_part(split_part(f.config->>'name','(',2),' ',1),
              trim(trailing ')' FROM split_part(split_part(f.config->>'name','(',2),' ',2)),
              latest.status,
              count(*)
            FROM latest JOIN feeds f ON f.id=latest.stream_id
            GROUP BY 1,2,3 ORDER BY 1,2,3;
            """
        )
        summary["outcomes"] = {
            f"{protocol}:{behavior}:{status}": int(count)
            for protocol, behavior, status, count in (
                line.split("\t") for line in outcomes.splitlines() if line
            )
        }
        alarms = self.psql(
            """
            SELECT
              split_part(split_part(f.config->>'name','(',2),' ',1),
              trim(trailing ')' FROM split_part(split_part(f.config->>'name','(',2),' ',2)),
              a.monitor_id,
              count(*)
            FROM current_alarms a JOIN feeds f ON f.id=a.stream_id
            WHERE a.active
            GROUP BY 1,2,3 ORDER BY 1,2,3;
            """
        )
        summary["activeAlarms"] = {
            f"{protocol}:{behavior}:{monitor_id}": int(count)
            for protocol, behavior, monitor_id, count in (
                line.split("\t") for line in alarms.splitlines() if line
            )
        }
        return summary

    def wait_for_durable_validation(
        self, started_at: str, expected_counts: dict[str, int]
    ) -> dict:
        deadline = time.monotonic() + self.timeout_seconds
        last_summary = {}
        last_errors = ["no durable summary"]
        while time.monotonic() < deadline:
            last_summary = self.durable_summary(started_at)
            last_errors = validate_durable_summary(
                last_summary, expected_counts, self.worker_count
            )
            if not last_errors:
                return last_summary
            time.sleep(5)
        (self.artifact_dir / "durable-summary.json").write_text(
            json.dumps(last_summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        raise RuntimeError("durable validation did not converge: " + "; ".join(last_errors))

    def capture_api_catalog(self) -> dict:
        feeds = []
        cursor = ""
        while True:
            query = urlencode({"limit": 200, "cursor": cursor})
            payload = self.container_json(f"/api/operator/feeds?{query}")
            feeds.extend(payload["feeds"])
            if not payload.get("hasMore"):
                break
            cursor = payload["nextCursor"]
        protocol_counts = dict(Counter(feed["protocol"] for feed in feeds))
        summary = {
            "apiVersion": "videosim.durable-fixture-api-catalog/v1",
            "feedCount": len(feeds),
            "distinctEndpointCount": len({feed["endpoint"] for feed in feeds}),
            "protocolCounts": protocol_counts,
            "feeds": feeds,
        }
        (self.artifact_dir / "api-catalog.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if summary["feedCount"] != self.expected_streams:
            raise RuntimeError("operator API returned an incomplete feed catalog")
        if summary["distinctEndpointCount"] != self.expected_streams:
            raise RuntimeError("operator API returned duplicate fixture endpoints")
        scenario = json.loads(self.scenario_path.read_text(encoding="utf-8"))
        expected_protocols = dict(
            Counter(stream["protocol"] for stream in scenario["streams"])
        )
        if protocol_counts != expected_protocols:
            raise RuntimeError("operator API protocol counts do not match the scenario")
        if any(feed["configVersion"] != 1 for feed in feeds):
            raise RuntimeError("operator API returned an unexpected config version")
        return summary

    def capture_docker_artifacts(self) -> list[str]:
        errors = []
        names = [self.postgres_name, self.app_name, *self.worker_names]
        inspect = self.run_command(
            ["docker", "inspect", *names],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        (self.artifact_dir / "container-state.json").write_text(
            inspect.stdout or "[]\n", encoding="utf-8"
        )
        if inspect.returncode:
            errors.append("Docker container inspection failed")
        else:
            states = json.loads(inspect.stdout)
            if any(
                item["State"]["Status"] != "running"
                or item.get("RestartCount", 0) != 0
                for item in states
            ):
                errors.append("a durable startup container is not stable")

        stats = self.run_command(
            ["docker", "stats", "--no-stream", "--format", "json", *names],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=60,
        )
        (self.artifact_dir / "docker-stats.jsonl").write_text(
            stats.stdout, encoding="utf-8"
        )
        if stats.returncode or not stats.stdout.strip():
            errors.append("Docker resource capture failed")

        log_path = self.artifact_dir / "docker.log"
        with log_path.open("w", encoding="utf-8") as output:
            for name in names:
                output.write(f"[{name}]\n")
                self.run_command(
                    ["docker", "logs", "--timestamps", name],
                    check=False,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    timeout=60,
                )
            output.write("[fixture-domain]\n")
            self.compose_fixture(
                "logs",
                "--no-color",
                "--timestamps",
                "srt",
                "dash",
                check=False,
                stdout=output,
                stderr=subprocess.STDOUT,
                timeout=60,
            )
        log_text = log_path.read_text(encoding="utf-8")
        if not log_text.strip():
            errors.append("Docker logs are empty")
        found = [marker for marker in CRITICAL_LOG_MARKERS if marker in log_text.lower()]
        if found:
            errors.append("Docker logs contain critical markers: " + ", ".join(found))
        return errors

    def run(self) -> dict:
        result = {
            "schemaVersion": "videosim.durable-fixture-startup/v1",
            "passed": False,
            "startedAt": datetime.now(timezone.utc).isoformat(),
            "project": self.project,
            "image": self.image,
            "immutableImage": "@sha256:" in self.image,
            "validationSurface": "api",
            "allPathMediaValidated": False,
            "capacityCertified": False,
            "independentHosts": False,
            "checks": [],
            "errors": [],
        }
        try:
            if not result["immutableImage"] and not self.allow_mutable_image:
                raise RuntimeError(
                    "VIDEOSIM_DURABLE_FIXTURE_IMAGE must use an immutable digest"
                )
            engine = self.run_command(
                ["docker", "info", "--format", "{{.OSType}}"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            ).stdout.strip()
            if engine != "linux":
                raise RuntimeError("durable fixture startup requires a Linux Docker engine")
            result["checks"].append("linux_docker_engine")
            self.cleanup()
            self.start_fixture()
            result["checks"].append("fixture_domain_started")
            self.start_postgres()
            result["checks"].append("postgres_started")
            self.migrate_and_import()
            result["checks"].append("catalog_migrated_imported")
            scenario = json.loads(self.scenario_path.read_text(encoding="utf-8"))
            expected_counts = expected_scenario_counts(scenario)
            if sum(expected_counts.values()) != self.expected_streams:
                raise RuntimeError("composed scenario count does not match its manifest")
            self.start_app()
            result["checks"].append("api_ready")
            worker_started_at = self.start_workers()
            result["checks"].append("workers_started")
            summary = self.wait_for_durable_validation(
                worker_started_at, expected_counts
            )
            (self.artifact_dir / "durable-summary.json").write_text(
                json.dumps(summary, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            result["durableSummary"] = summary
            result["checks"].append("durable_media_alarms_validated")
            api_catalog = self.capture_api_catalog()
            result["apiCatalog"] = {
                key: value for key, value in api_catalog.items() if key != "feeds"
            }
            result["checks"].append("operator_api_catalog_validated")
            result["allPathMediaValidated"] = True
            result["passed"] = True
        except Exception as exc:
            result["errors"].append(str(exc))
        finally:
            try:
                artifact_errors = self.capture_docker_artifacts()
                result["errors"].extend(artifact_errors)
                if artifact_errors:
                    result["passed"] = False
                else:
                    result["checks"].append("docker_state_stats_logs_captured")
            except Exception as exc:
                result["errors"].append(f"artifact capture failed: {exc}")
                result["passed"] = False
            if not self.keep:
                try:
                    self.cleanup()
                except Exception as exc:
                    result["errors"].append(f"cleanup failed: {exc}")
                    result["passed"] = False
            result["endedAt"] = datetime.now(timezone.utc).isoformat()
            (self.artifact_dir / "result.json").write_text(
                json.dumps(result, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        return result


def main() -> int:
    startup = DurableFixtureStartup()
    result = startup.run()
    print(json.dumps(result, indent=2, sort_keys=True))
    print(f"Artifacts: {startup.artifact_dir}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
