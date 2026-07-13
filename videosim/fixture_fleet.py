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

from .feed import require_gst_launch


FIXTURE_SCHEMA = "videosim.fixture-fleet/v1"
BEHAVIORS = ("healthy", "slow", "dead", "malformed")


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
        ("httpPort", 1, 65535) if protocol == "dash" else ("basePort", 1, 65532)
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
    return manifest, hashlib.sha256(raw).hexdigest()


def fixture_state(manifest: dict, manifest_sha256: str) -> dict:
    protocol = manifest["protocol"]
    if protocol == "dash":
        base_url = f"http://{manifest['advertisedHost']}:{manifest['httpPort']}"
        endpoints = [f"{base_url}/{behavior}/manifest.mpd" for behavior in BEHAVIORS]
    else:
        endpoints = [
            f"srt://{manifest['advertisedHost']}:{manifest['basePort'] + index}?mode=caller"
            for index in range(len(BEHAVIORS))
        ]
    streams = [
        {
            "id": f"{protocol}-{behavior}",
            "name": f"{protocol.upper()} {behavior} fixture",
            "protocol": protocol,
            "source": "external",
            "mode": "normal",
            "status": "running",
            "endpoint": endpoint,
            "width": manifest["width"],
            "height": manifest["height"],
            "framerate": str(manifest["framerate"]),
        }
        for behavior, endpoint in zip(BEHAVIORS, endpoints)
    ]
    return {
        "schemaVersion": "videosim.fixture-state/v1",
        "fixtureManifestSha256": manifest_sha256,
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
            target = (root / Path(*parts[1:])).resolve()
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
    base_port = manifest["basePort"]

    def listener(port: int) -> list[str]:
        return [
            "!",
            "srtsink",
            f"uri=srt://:{port}?mode=listener",
            "wait-for-connection=false",
        ]

    healthy = [
        sys.executable,
        "-m",
        "videosim",
        "start",
        "--profile",
        manifest["profile"],
        "--protocol",
        "srt",
        "--port",
        str(base_port),
        "--width",
        str(manifest["width"]),
        "--height",
        str(manifest["height"]),
        "--framerate",
        str(manifest["framerate"]),
    ]
    slow = [
        gst_launch,
        "-q",
        "fakesrc",
        "is-live=true",
        "do-timestamp=true",
        "sizetype=fixed",
        "sizemax=188",
        "filltype=zero",
        "!",
        "identity",
        f"sleep-time={int(manifest['slowDelaySeconds'] * 1_000_000)}",
        *listener(base_port + 1),
    ]
    malformed = [
        gst_launch,
        "-q",
        "fakesrc",
        "is-live=true",
        "do-timestamp=true",
        "sizetype=fixed",
        "sizemax=188",
        "filltype=pattern",
        "datarate=1880",
        "sync=true",
        *listener(base_port + 3),
    ]
    return [healthy, slow, malformed]


def run_srt_fixture_fleet(manifest: dict, state_path: str, manifest_sha256: str) -> int:
    processes = []
    try:
        for command in srt_fixture_commands(manifest):
            processes.append(subprocess.Popen(command))
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
