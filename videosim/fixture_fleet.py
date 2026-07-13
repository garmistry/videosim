from __future__ import annotations

import hashlib
import json
import mimetypes
import signal
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from .control_plane import MAX_REPORT_STREAMS
from .feed import require_gst_launch


FIXTURE_SCHEMA = "videosim.fixture-fleet/v1"
BEHAVIORS = ("healthy", "slow", "dead", "malformed")
# ponytail: fixed batching; tune only from fixture-host media saturation evidence.
SRT_FANOUT_SIZE = 16
SRT_MULTICAST_GROUP = "239.255.42.42"


def behavior_endpoint_counts(manifest: dict) -> dict[str, int]:
    return manifest.get("behaviorEndpointCounts") or {
        behavior: 1 for behavior in BEHAVIORS
    }


def load_fixture_manifest(path: str | Path) -> tuple[dict, str]:
    manifest_path = Path(path)
    try:
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"fixture fleet manifest is invalid: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("fixture fleet manifest must be an object")
    if manifest.get("schemaVersion") != FIXTURE_SCHEMA:
        raise ValueError(f"fixture fleet schemaVersion must be {FIXTURE_SCHEMA}")
    protocol = manifest.get("protocol")
    if protocol not in {"dash", "srt"}:
        raise ValueError("fixture fleet protocol must be dash or srt")
    string_fields = ["advertisedHost", "profile"]
    if protocol == "dash":
        string_fields.extend(("bindHost", "dashDir"))
    for field in string_fields:
        if not isinstance(manifest.get(field), str) or not manifest[field].strip():
            raise ValueError(f"fixture fleet {field} must be a non-empty string")
    integer_fields = [("width", 1, 7680), ("height", 1, 4320)]
    integer_fields.append(
        ("httpPort", 1, 65535) if protocol == "dash" else ("basePort", 1, 65535)
    )
    for field, minimum, maximum in integer_fields:
        value = manifest.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(
                f"fixture fleet {field} must be an integer from {minimum} to {maximum}"
            )
    for field, maximum in (("framerate", 240), ("slowDelaySeconds", 3600)):
        value = manifest.get(field)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 < value <= maximum
        ):
            raise ValueError(
                f"fixture fleet {field} must be a positive number no greater than {maximum}"
            )
    counts = manifest.get("behaviorEndpointCounts")
    if counts is not None:
        if not isinstance(counts, dict) or set(counts) != set(BEHAVIORS):
            raise ValueError(
                "fixture fleet behaviorEndpointCounts must contain exactly "
                + ", ".join(BEHAVIORS)
            )
        for behavior, count in counts.items():
            if (
                isinstance(count, bool)
                or not isinstance(count, int)
                or not 1 <= count <= MAX_REPORT_STREAMS
            ):
                raise ValueError(
                    f"fixture fleet behaviorEndpointCounts.{behavior} must be an integer "
                    f"from 1 to {MAX_REPORT_STREAMS}"
                )
        if sum(counts.values()) > MAX_REPORT_STREAMS:
            raise ValueError(
                f"fixture fleet behaviorEndpointCounts total must not exceed {MAX_REPORT_STREAMS}"
            )
    counts = behavior_endpoint_counts(manifest)
    required_ports = sum(counts.values()) + (2 if counts["healthy"] > 1 else 0)
    if protocol == "srt" and manifest["basePort"] + required_ports > 65536:
        raise ValueError("fixture fleet basePort plus endpoint count exceeds 65535")
    return manifest, hashlib.sha256(raw).hexdigest()


def fixture_state(manifest: dict, manifest_sha256: str) -> dict:
    protocol = manifest["protocol"]
    counts = behavior_endpoint_counts(manifest)
    expanded = "behaviorEndpointCounts" in manifest
    streams = []
    offset = 0
    for behavior in BEHAVIORS:
        for index in range(counts[behavior]):
            endpoint_number = offset + index
            endpoint_id = f"endpoint-{index + 1:05d}"
            if protocol == "dash":
                endpoint_path = f"/{behavior}/{endpoint_id}" if expanded else f"/{behavior}"
                endpoint = (
                    f"http://{manifest['advertisedHost']}:{manifest['httpPort']}"
                    f"{endpoint_path}/manifest.mpd"
                )
            else:
                endpoint = (
                    f"srt://{manifest['advertisedHost']}:"
                    f"{manifest['basePort'] + endpoint_number}?mode=caller"
                )
            suffix = f"-{index + 1:05d}" if expanded else ""
            streams.append(
                {
                    "id": f"{protocol}-{behavior}{suffix}",
                    "name": f"{protocol.upper()} {behavior} fixture{suffix}",
                    "protocol": protocol,
                    "source": "external",
                    "mode": "normal",
                    "status": "running",
                    "endpoint": endpoint,
                    "fixtureBehavior": behavior,
                    "width": manifest["width"],
                    "height": manifest["height"],
                    "framerate": str(manifest["framerate"]),
                }
            )
        offset += counts[behavior]
    return {
        "schemaVersion": "videosim.fixture-state/v1",
        "fixtureManifestSha256": manifest_sha256,
        "behaviorEndpointCounts": counts,
        "streams": streams,
    }


def build_dash_fixture_handler(dash_dir: str | Path, slow_delay_seconds: float):
    root = Path(dash_dir).resolve()

    class DashFixtureHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/healthz":
                return self._send(200, b"ok\n", "text/plain")
            parts = [unquote(part) for part in path.split("/") if part]
            if len(parts) < 2 or parts[0] not in BEHAVIORS:
                return self._send(404, b"not found\n", "text/plain")
            behavior = parts[0]
            if behavior == "dead":
                return self._send(503, b"fixture unavailable\n", "text/plain")
            if behavior == "malformed":
                return self._send(200, b"<MPD><broken", "application/dash+xml")
            if behavior == "slow":
                time.sleep(slow_delay_seconds)
            content_parts = (
                parts[2:]
                if len(parts) > 2 and parts[1].startswith("endpoint-")
                else parts[1:]
            )
            target = (root / Path(*content_parts)).resolve()
            if target != root and root not in target.parents:
                return self._send(404, b"not found\n", "text/plain")
            try:
                body = target.read_bytes()
            except OSError:
                return self._send(404, b"not ready\n", "text/plain")
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            return self._send(200, body, content_type)

        def _send(self, status: int, body: bytes, content_type: str):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, _format, *_args):
            return

    return DashFixtureHandler


def write_fixture_state(path: str | Path, state: dict):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(target)


def stop_process(process: subprocess.Popen):
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def srt_fixture_commands(manifest: dict) -> list[list[str]]:
    gst_launch = require_gst_launch()
    counts = behavior_endpoint_counts(manifest)
    ports = {}
    offset = 0
    for behavior in BEHAVIORS:
        ports[behavior] = range(
            manifest["basePort"] + offset,
            manifest["basePort"] + offset + counts[behavior],
        )
        offset += counts[behavior]

    def listener(port: int) -> list[str]:
        return [
            "!",
            "srtsink",
            f"uri=srt://:{port}?mode=listener",
            "wait-for-connection=false",
            "async=false",
        ]

    def fanout(source: list[str], listener_ports: list[int]) -> list[str]:
        command = [gst_launch, "-q", *source, "!", "tee", "name=fanout"]
        for port in listener_ports:
            command.extend(
                [
                    "fanout.",
                    "!",
                    "queue",
                    *listener(port),
                ]
            )
        return command

    def batches(listener_ports: range) -> list[list[int]]:
        values = list(listener_ports)
        return [
            values[offset : offset + SRT_FANOUT_SIZE]
            for offset in range(0, len(values), SRT_FANOUT_SIZE)
        ]

    def feed(port: int) -> list[str]:
        return [
            sys.executable,
            "-m",
            "videosim",
            "start",
            "--profile",
            manifest["profile"],
            "--protocol",
            "srt",
            "--port",
            str(port),
            "--width",
            str(manifest["width"]),
            "--height",
            str(manifest["height"]),
            "--framerate",
            str(manifest["framerate"]),
        ]

    healthy_ports = ports["healthy"]
    if len(healthy_ports) == 1:
        commands = [feed(healthy_ports[0])]
    else:
        source_port = manifest["basePort"] + sum(counts.values())
        multicast_port = source_port + 1
        commands = [
            feed(source_port),
            [
                gst_launch,
                "-q",
                "srtsrc",
                f"uri=srt://127.0.0.1:{source_port}?mode=caller",
                "blocksize=1316",
                "!",
                "udpsink",
                f"host={SRT_MULTICAST_GROUP}",
                f"port={multicast_port}",
                "auto-multicast=true",
                "sync=false",
                "async=false",
            ],
        ]
        healthy_source = [
            "udpsrc",
            f"address={SRT_MULTICAST_GROUP}",
            f"port={multicast_port}",
            "auto-multicast=true",
            "caps=video/mpegts,systemstream=(boolean)true,packetsize=(int)188",
        ]
        commands.extend(
            fanout(healthy_source, batch) for batch in batches(healthy_ports)
        )
    slow_source = [
        "fakesrc",
        "is-live=true",
        "do-timestamp=true",
        "sizetype=fixed",
        "sizemax=188",
        "filltype=zero",
        "!",
        "identity",
        f"sleep-time={int(manifest['slowDelaySeconds'] * 1_000_000)}",
    ]
    commands.extend(
        fanout(slow_source, batch) for batch in batches(ports["slow"])
    )
    malformed_source = [
        "fakesrc",
        "is-live=true",
        "do-timestamp=true",
        "sizetype=fixed",
        "sizemax=188",
        "filltype=pattern",
        "datarate=1880",
        "sync=true",
    ]
    commands.extend(
        fanout(malformed_source, batch)
        for batch in batches(ports["malformed"])
    )
    return commands


def run_srt_fixture_fleet(manifest: dict, state_path: str, manifest_sha256: str) -> int:
    processes = []
    try:
        commands = srt_fixture_commands(manifest)
        for index, command in enumerate(commands):
            processes.append(subprocess.Popen(command))
            if index == 0 and behavior_endpoint_counts(manifest)["healthy"] > 1:
                # ponytail: fixed local barrier; use SRT readiness signaling for remote startup.
                time.sleep(2)
                if processes[0].poll() is not None:
                    raise ValueError(
                        f"SRT fixture source exited with {processes[0].returncode}"
                    )
        time.sleep(0.5)
        for process in processes:
            if process.poll() is not None:
                raise ValueError(f"SRT fixture process exited with {process.returncode}")
        write_fixture_state(state_path, fixture_state(manifest, manifest_sha256))
        print(f"SRT fixture fleet ready: state={state_path}", flush=True)
        while True:
            for process in processes:
                if process.poll() is not None:
                    raise ValueError(
                        f"SRT fixture process exited with {process.returncode}"
                    )
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        for process in reversed(processes):
            stop_process(process)


def run_fixture_fleet(
    manifest_path: str, state_path: str, advertised_host: str = ""
) -> int:
    manifest, manifest_sha256 = load_fixture_manifest(manifest_path)
    advertised_host = advertised_host.strip()
    if advertised_host:
        manifest = {**manifest, "advertisedHost": advertised_host}
    if manifest["protocol"] == "srt":
        return run_srt_fixture_fleet(manifest, state_path, manifest_sha256)
    dash_dir = Path(manifest["dashDir"])
    dash_dir.mkdir(parents=True, exist_ok=True)
    base_url = f"http://{manifest['advertisedHost']}:{manifest['httpPort']}/healthy"
    generator = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "videosim",
            "start",
            "--profile",
            manifest["profile"],
            "--protocol",
            "dash",
            "--width",
            str(manifest["width"]),
            "--height",
            str(manifest["height"]),
            "--framerate",
            str(manifest["framerate"]),
            "--dash-dir",
            str(dash_dir),
            "--dash-base-url",
            base_url,
        ]
    )
    try:
        server = ThreadingHTTPServer(
            (manifest["bindHost"], manifest["httpPort"]),
            build_dash_fixture_handler(dash_dir, manifest["slowDelaySeconds"]),
        )
    except Exception:
        stop_process(generator)
        raise
    server.daemon_threads = True
    server.block_on_close = False
    server.timeout = 0.5
    write_fixture_state(state_path, fixture_state(manifest, manifest_sha256))
    print(f"DASH fixture fleet ready: state={state_path}", flush=True)
    try:
        while generator.poll() is None:
            server.handle_request()
        raise ValueError(f"DASH fixture generator exited with {generator.returncode}")
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
        stop_process(generator)
