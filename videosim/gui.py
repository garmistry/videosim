from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import html
import json
import mimetypes
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote, unquote, urlparse


PROFILE_OPTIONS = {
    "normal": ("Normal", "profiles/srt-normal.yaml"),
    "audio_only": ("Audio only", "profiles/srt-audio-only.yaml"),
    "video_only": ("Video only", "profiles/srt-video-only.yaml"),
    "no_captions": ("No captions", "profiles/srt-no-captions.yaml"),
    "black_video": ("Black video", "profiles/srt-black-video.yaml"),
    "frozen_video": ("Frozen video", "profiles/srt-frozen-video.yaml"),
}

PROTOCOL_OPTIONS = {"srt": "SRT", "dash": "DASH"}

CONTROL_FIELDS = ("video", "audio", "captions", "black_video", "frozen_video")
CONTROL_FORM_FIELD = "controls"

MODE_CONTROLS = {
    "normal": {"video": True, "audio": True, "captions": True, "black_video": False, "frozen_video": False},
    "audio_only": {"video": False, "audio": True, "captions": False, "black_video": False, "frozen_video": False},
    "video_only": {"video": True, "audio": False, "captions": True, "black_video": False, "frozen_video": False},
    "no_captions": {"video": True, "audio": True, "captions": False, "black_video": False, "frozen_video": False},
    "black_video": {"video": True, "audio": True, "captions": True, "black_video": True, "frozen_video": False},
    "frozen_video": {"video": True, "audio": True, "captions": True, "black_video": False, "frozen_video": True},
}

STATIC_DIR = Path(__file__).resolve().parent / "static"


def controls_for_mode(mode: str) -> dict[str, bool]:
    return dict(MODE_CONTROLS.get(mode, MODE_CONTROLS["normal"]))


def mode_from_controls(controls: dict[str, bool]) -> str:
    normalized = {field: bool(controls.get(field)) for field in CONTROL_FIELDS}
    for mode, expected in MODE_CONTROLS.items():
        if normalized == expected:
            return mode

    if normalized["black_video"] and normalized["frozen_video"]:
        raise ValueError("Black video and frozen video cannot both be enabled")
    if not normalized["video"] and not normalized["audio"]:
        raise ValueError("Video and audio cannot both be disabled")
    if not normalized["video"] and (normalized["captions"] or normalized["black_video"] or normalized["frozen_video"]):
        raise ValueError("Video-dependent faults require video to be enabled")
    raise ValueError("Unsupported toggle combination")


def mode_from_form(params: dict[str, list[str]], default_mode: str) -> str:
    if CONTROL_FORM_FIELD in params or any(field in params for field in CONTROL_FIELDS):
        return mode_from_controls({field: field in params for field in CONTROL_FIELDS})
    return params.get("mode", [default_mode])[0]


def verbose_enabled() -> bool:
    return os.environ.get("VIDEOSIM_VERBOSE", "").lower() in {"1", "true", "yes", "on"}


def running_in_container() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def feed_path(stream_id: str) -> str:
    return f"/feeds/{quote(stream_id, safe='')}"


@dataclass
class FeedRecord:
    id: str
    name: str
    protocol: str
    http_port: int
    feed_port: int
    width: int
    height: int
    framerate: int
    dash_dir: str
    mode: str
    process: subprocess.Popen | None = None
    logs: list[str] = field(default_factory=list)
    last_error: str = ""
    validation_output: str = ""

    @property
    def endpoint(self) -> str:
        if self.protocol == "dash":
            return f"http://127.0.0.1:{self.http_port}/dash/{self.id}/manifest.mpd"
        return f"srt://127.0.0.1:{self.feed_port}?mode=caller"

    @property
    def status(self) -> str:
        if self.process and self.process.poll() is None:
            return "running"
        return "stopped"

    @property
    def intentional_outage(self) -> bool:
        return self.mode != "normal"


@dataclass
class GuiState:
    protocol: str = "srt"
    http_port: int = 8080
    feed_port: int = 9000
    width: int = 1280
    height: int = 720
    framerate: int = 30
    dash_dir: str = "/tmp/videosim-dash"
    mode: str = "normal"
    process: subprocess.Popen | None = None
    logs: list[str] = field(default_factory=list)
    last_error: str = ""
    validation_output: str = ""
    streams: dict[str, FeedRecord] = field(default_factory=dict)
    selected_stream_id: str = ""
    _next_stream_number: int = 1

    def __post_init__(self):
        if self.streams and not self.selected_stream_id:
            self.select_stream(next(iter(self.streams)))

    @property
    def active_stream(self) -> FeedRecord | None:
        if not self.streams or not self.selected_stream_id:
            return None
        if self.selected_stream_id not in self.streams:
            self.selected_stream_id = ""
            return None
        return self.streams[self.selected_stream_id]

    @property
    def endpoint(self) -> str:
        stream = self.active_stream
        return stream.endpoint if stream else ""

    @property
    def status(self) -> str:
        stream = self.active_stream
        if not stream:
            return "stopped"
        if stream.process is None and self.process and self.process.poll() is None:
            return "running"
        return stream.status

    @property
    def intentional_outage(self) -> bool:
        stream = self.active_stream
        return stream.intentional_outage if stream else False

    def create_stream(
        self,
        name: str = "",
        protocol: str = "srt",
        mode: str = "normal",
        feed_port: int | None = None,
        width: int | None = None,
        height: int | None = None,
        framerate: int | None = None,
        select: bool = True,
    ) -> FeedRecord:
        if protocol not in PROTOCOL_OPTIONS:
            raise ValueError(f"Unsupported protocol: {protocol}")
        if mode not in PROFILE_OPTIONS:
            raise ValueError(f"Unsupported mode: {mode}")
        stream_id = f"stream-{self._next_stream_number}"
        self._next_stream_number += 1
        feed_port = feed_port or self.next_available_port()
        stream = FeedRecord(
            id=stream_id,
            name=name.strip() or f"Feed {len(self.streams) + 1}",
            protocol=protocol,
            http_port=self.http_port,
            feed_port=feed_port,
            width=width or self.width,
            height=height or self.height,
            framerate=framerate or self.framerate,
            dash_dir=str(Path(self.dash_dir) / stream_id),
            mode=mode,
        )
        self.streams[stream_id] = stream
        if select:
            self.select_stream(stream_id)
        return stream

    def next_available_port(self) -> int:
        used = {stream.feed_port for stream in self.streams.values()}
        port = self.feed_port
        while port in used:
            port += 1
        return port

    def select_stream(self, stream_id: str) -> bool:
        if stream_id not in self.streams:
            self.fail(f"Unsupported stream: {stream_id}")
            return False
        self.selected_stream_id = stream_id
        self._sync_from_active()
        return True

    def clear_selection(self):
        self.selected_stream_id = ""
        self._sync_from_active()

    def update_stream(self, stream_id: str, name: str | None = None, protocol: str | None = None, mode: str | None = None) -> bool:
        if stream_id not in self.streams:
            self.fail(f"Unsupported stream: {stream_id}")
            return False
        stream = self.streams[stream_id]
        restart = stream.status == "running"
        if restart:
            self.stop(stream_id)
        if name is not None and name.strip():
            stream.name = name.strip()
        if protocol is not None:
            if protocol not in PROTOCOL_OPTIONS:
                self.fail(f"Unsupported protocol: {protocol}")
                return False
            stream.protocol = protocol
        if mode is not None:
            if mode not in PROFILE_OPTIONS:
                self.fail(f"Unsupported mode: {mode}")
                return False
            stream.mode = mode
        self.select_stream(stream_id)
        if restart:
            return self.start(stream_id)
        return True

    def delete_stream(self, stream_id: str) -> bool:
        if stream_id not in self.streams:
            self.fail(f"Unsupported stream: {stream_id}")
            return False
        self.stop(stream_id)
        del self.streams[stream_id]
        if self.selected_stream_id == stream_id:
            self.selected_stream_id = ""
        if self.streams and not self.selected_stream_id:
            self.select_stream(next(iter(self.streams)))
        elif not self.streams:
            self._sync_from_active()
        return True

    def stop_all(self):
        for stream_id in list(self.streams):
            self.stop(stream_id)

    def _sync_from_active(self):
        stream = self.active_stream
        if not stream:
            self.process = None
            self.logs = []
            self.last_error = ""
            self.validation_output = ""
            return
        self.protocol = stream.protocol
        self.feed_port = stream.feed_port
        self.width = stream.width
        self.height = stream.height
        self.framerate = stream.framerate
        self.mode = stream.mode
        self.process = stream.process
        self.logs = stream.logs
        self.last_error = stream.last_error
        self.validation_output = stream.validation_output

    def start(self, stream_id: str | None = None):
        if stream_id:
            self.select_stream(stream_id)
        stream = self.active_stream
        if not stream:
            self.fail("Create a feed before starting")
            return False
        if stream.status == "running":
            self.log("Feed already running", stream.id)
            return False
        if stream.mode not in PROFILE_OPTIONS:
            self.fail(f"Unsupported mode: {stream.mode}", stream.id)
            return False
        profile = profile_for(stream.protocol, stream.mode)
        cmd = [
            sys.executable,
            "-m",
            "videosim",
            "start",
            "--profile",
            profile,
            "--port",
            str(stream.feed_port),
            "--width",
            str(stream.width),
            "--height",
            str(stream.height),
            "--framerate",
            str(stream.framerate),
        ]
        if stream.protocol == "dash":
            cmd.extend(
                [
                    "--protocol",
                    "dash",
                    "--dash-dir",
                    stream.dash_dir,
                    "--dash-base-url",
                    f"http://127.0.0.1:{self.http_port}/dash/{stream.id}",
                ]
            )
        stream.last_error = ""
        container = "yes" if running_in_container() else "no"
        self.log(
            f"Starting {stream.protocol} {stream.mode} feed: profile={profile} endpoint={stream.endpoint} container={container}",
            stream.id,
        )
        if verbose_enabled():
            self.log(f"Feed launch command: {shlex.join(cmd)}", stream.id)
        try:
            stream.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        except OSError as exc:
            stream.process = None
            self.fail(f"Failed to start {stream.mode} feed: {exc}", stream.id)
            return False
        self._sync_from_active()
        self.log(f"Started {stream.protocol} {stream.mode} feed at {stream.endpoint} pid={stream.process.pid}", stream.id)
        threading.Thread(target=self._capture_logs, args=(stream.id, stream.process), daemon=True).start()
        return True

    def apply_mode(self, mode: str, protocol: str | None = None, stream_id: str | None = None):
        if stream_id:
            self.select_stream(stream_id)
        stream = self.active_stream
        if not stream:
            self.fail("Create a feed before starting")
            return False
        protocol = protocol or self.protocol
        if protocol not in PROTOCOL_OPTIONS:
            self.fail(f"Unsupported protocol: {protocol}", stream.id)
            return False
        if mode not in PROFILE_OPTIONS:
            self.fail(f"Unsupported mode: {mode}", stream.id)
            return False
        if stream.status == "running" and mode == stream.mode and protocol == stream.protocol:
            self.log("Feed already running", stream.id)
            return True
        if stream.status == "running":
            self.log(f"Restarting feed for {protocol} {mode} mode", stream.id)
            self.stop(stream.id)
        stream.mode = mode
        stream.protocol = protocol
        self._sync_from_active()
        return self.start(stream.id)

    def validate(self, stream_id: str | None = None):
        if stream_id:
            self.select_stream(stream_id)
        stream = self.active_stream
        if not stream:
            self.fail("Create a feed before validating")
            return False
        if stream.mode not in PROFILE_OPTIONS:
            self.fail(f"Unsupported mode: {stream.mode}", stream.id)
            return False
        profile = profile_for(stream.protocol, stream.mode)
        cmd = [
            sys.executable,
            "-m",
            "videosim",
            "validate",
            "--profile",
            profile,
            "--port",
            str(stream.feed_port),
            "--width",
            str(stream.width),
            "--height",
            str(stream.height),
            "--framerate",
            str(stream.framerate),
        ]
        if stream.protocol == "dash":
            cmd.extend(
                [
                    "--protocol",
                    "dash",
                    "--dash-dir",
                    stream.dash_dir,
                    "--dash-base-url",
                    f"http://127.0.0.1:{self.http_port}/dash/{stream.id}",
                ]
            )
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=45,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            stream.validation_output = ""
            self.fail(f"Validation failed: {exc}", stream.id)
            return False
        stream.validation_output = result.stdout.strip()
        self.log("Validation passed" if result.returncode == 0 else "Validation failed", stream.id)
        if result.returncode != 0:
            stream.last_error = stream.validation_output.splitlines()[-1] if stream.validation_output else "Validation failed"
        self._sync_from_active()
        return result.returncode == 0

    def stop(self, stream_id: str | None = None):
        if stream_id:
            self.select_stream(stream_id)
        stream = self.active_stream
        if not stream:
            self.log("No feeds configured")
            return
        if not stream.process or stream.process.poll() is not None:
            stream.process = None
            self._sync_from_active()
            self.log("Feed already stopped", stream.id)
            return
        process = stream.process
        self.log("Stopping feed", stream.id)
        os.killpg(process.pid, signal.SIGINT)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            self.log("Killed stuck feed", stream.id)
        stream.process = None
        self._sync_from_active()
        self.log("Feed stopped", stream.id)

    def fail(self, message: str, stream_id: str | None = None):
        stream = self.streams.get(stream_id or self.selected_stream_id)
        if stream:
            stream.last_error = message
        self.last_error = message
        self.log(message, stream_id)

    def log(self, message: str, stream_id: str | None = None):
        stream = self.streams.get(stream_id or self.selected_stream_id)
        if stream:
            if self.validation_output and not stream.validation_output:
                stream.validation_output = self.validation_output
            if self.last_error and not stream.last_error:
                stream.last_error = self.last_error
            stream.logs.append(message)
            del stream.logs[:-200]
            if stream.id == self.selected_stream_id:
                self._sync_from_active()
        else:
            self.logs.append(message)
            del self.logs[:-200]
        if verbose_enabled() or running_in_container():
            print(f"[videosim-gui] {message}", flush=True)

    def _capture_logs(self, stream_id: str | subprocess.Popen, process=None):
        if process is None:
            process = stream_id
            stream_id = self.selected_stream_id
        if not process.stdout:
            return
        last_line = ""
        for line in process.stdout:
            last_line = line.rstrip()
            self.log(last_line, stream_id)
        code = process.poll()
        if code not in (None, 0):
            detail = f": {last_line}" if last_line else ""
            self.fail(f"Feed process exited with code {code}{detail}", stream_id)


def profile_for(protocol: str, mode: str) -> str:
    if protocol == "srt":
        return PROFILE_OPTIONS[mode][1]
    if protocol == "dash":
        return f"profiles/dash-{mode.replace('_', '-')}.yaml"
    raise ValueError(f"Unsupported protocol: {protocol}")


class GuiHandler(BaseHTTPRequestHandler):
    state: GuiState

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/diagnostics.txt":
            self._send_text(diagnostics_text(self.state))
            return
        if path == "/state.json":
            self._send_json(state_payload(self.state))
            return
        if path == "/preview.jpg":
            body, content_type = preview_image(self.state)
            self._send_bytes(body, content_type)
            return
        if path.startswith("/dash/"):
            self._send_dash(path.removeprefix("/dash/"))
            return
        if path.startswith("/static/"):
            self._send_static(path.removeprefix("/static/"))
            return
        if path.startswith("/feeds/"):
            stream_id = unquote(path.removeprefix("/feeds/").strip("/"))
            if not stream_id or not self.state.select_stream(stream_id):
                self.send_error(404)
                return
            self._send_html(render_page(self.state))
            return
        if path != "/":
            self.send_error(404)
            return
        self.state.clear_selection()
        self._send_html(render_page(self.state))

    def do_POST(self):
        redirect_stream_id = self.state.selected_stream_id
        if self.path == "/start":
            params = self._read_form()
            try:
                stream_id = params.get("stream_id", [self.state.selected_stream_id])[0]
                if stream_id:
                    self.state.select_stream(stream_id)
                    redirect_stream_id = stream_id
                mode = mode_from_form(params, self.state.mode)
                protocol = params.get("protocol", [self.state.protocol])[0]
            except ValueError as exc:
                self.state.log(str(exc))
                self._redirect_stream(redirect_stream_id)
                return
            self.state.apply_mode(mode, protocol, stream_id)
        elif self.path == "/stop":
            params = self._read_form()
            redirect_stream_id = params.get("stream_id", [self.state.selected_stream_id])[0]
            self.state.stop(redirect_stream_id or None)
        elif self.path == "/validate":
            params = self._read_form()
            redirect_stream_id = params.get("stream_id", [self.state.selected_stream_id])[0]
            self.state.validate(redirect_stream_id or None)
        elif self.path == "/streams/create":
            params = self._read_form()
            try:
                stream = self.state.create_stream(
                    name=params.get("name", [""])[0],
                    protocol=params.get("protocol", ["srt"])[0],
                    mode=params.get("mode", ["normal"])[0],
                )
                redirect_stream_id = stream.id
            except ValueError as exc:
                self.state.log(str(exc))
        elif self.path == "/streams/select":
            params = self._read_form()
            stream_id = params.get("stream_id", [self.state.selected_stream_id])[0]
            if stream_id:
                self.state.select_stream(stream_id)
                redirect_stream_id = stream_id
        elif self.path == "/streams/update":
            params = self._read_form()
            stream_id = params.get("stream_id", [self.state.selected_stream_id])[0]
            if stream_id:
                redirect_stream_id = stream_id
                try:
                    self.state.update_stream(
                        stream_id,
                        name=params.get("name", [None])[0],
                        protocol=params.get("protocol", [None])[0],
                        mode=params.get("mode", [None])[0],
                    )
                except ValueError as exc:
                    self.state.log(str(exc))
        elif self.path == "/streams/delete":
            params = self._read_form()
            stream_id = params.get("stream_id", [self.state.selected_stream_id])[0]
            if stream_id:
                self.state.delete_stream(stream_id)
                redirect_stream_id = self.state.selected_stream_id
        else:
            self.send_error(404)
            return
        self._redirect_stream(redirect_stream_id)

    def log_message(self, format, *args):
        return

    def _send_html(self, body: str):
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_text(self, body: str):
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Disposition", "attachment; filename=videosim-diagnostics.txt")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(self, payload):
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_bytes(self, body: bytes, content_type: str):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, relative_path: str):
        path = (STATIC_DIR / relative_path).resolve()
        try:
            path.relative_to(STATIC_DIR)
        except ValueError:
            self.send_error(404)
            return
        if not path.is_file():
            self.send_error(404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_dash(self, relative_path: str):
        parts = relative_path.split("/", 1)
        stream = self.state.streams.get(parts[0])
        if stream:
            relative_path = parts[1] if len(parts) > 1 else ""
        else:
            stream = self.state.active_stream
        if not stream:
            self.send_error(404)
            return
        path = (Path(stream.dash_dir) / relative_path).resolve()
        root = Path(stream.dash_dir).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            self.send_error(404)
            return
        if not path.is_file():
            self.send_error(404)
            return
        content_type = "application/dash+xml" if path.suffix == ".mpd" else mimetypes.guess_type(path.name)[0]
        if path.suffix == ".ts":
            content_type = "video/mp2t"
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect_home(self):
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def _redirect_stream(self, stream_id: str | None = None):
        if stream_id and stream_id in self.state.streams:
            self.send_response(303)
            self.send_header("Location", feed_path(stream_id))
            self.end_headers()
            return
        self._redirect_home()

    def _read_form(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8") if length else ""
        return parse_qs(body)


def render_page(state: GuiState) -> str:
    active = state.active_stream
    logs_source = state.logs or (active.logs if active else [])
    logs = "\n".join(html.escape(line) for line in logs_source[-80:])
    status = html.escape(state.status)
    endpoint = html.escape(state.endpoint)
    outage = "yes" if state.intentional_outage else "no"
    last_error = html.escape(state.last_error or (active.last_error if active else "") or "none")
    validation = html.escape(state.validation_output or (active.validation_output if active else "") or "not run")
    preview = (
        '<h2>Preview</h2><img alt="SRT stream preview" src="/preview.jpg" style="width:min(100%,40rem);border-radius:0.5rem;background:#101820;">'
        if state.status == "running"
        else ""
    )
    options = "\n".join(
        f'<option value="{html.escape(mode)}"{" selected" if mode == state.mode else ""}>{html.escape(label)}</option>'
        for mode, (label, _) in PROFILE_OPTIONS.items()
    )
    protocol_options = "\n".join(
        f'<option value="{html.escape(protocol)}"{" selected" if protocol == state.protocol else ""}>{html.escape(label)}</option>'
        for protocol, label in PROTOCOL_OPTIONS.items()
    )
    controls = controls_for_mode(active.mode if active else state.mode)
    checked = {field: " checked" if controls[field] else "" for field in CONTROL_FIELDS}
    stream_rows = "\n".join(
        f"""<li>
          <div class="row">
            <a class="button secondary" href="{html.escape(feed_path(stream.id))}">Open</a>
            <strong>{html.escape(stream.name)}</strong>
            <span>{html.escape(stream.protocol.upper())}</span>
            <span>{html.escape(stream.mode)}</span>
            <span>{html.escape(stream.status)}</span>
          </div>
        </li>"""
        for stream in state.streams.values()
    )
    create_form = f"""
  <form method="post" action="/streams/create" class="row">
    <label>Name <input name="name" value="Feed {len(state.streams) + 1}"></label>
    <label>Protocol <select name="protocol">{protocol_options}</select></label>
    <label>Mode <select name="mode">{options}</select></label>
    <button type="submit">Create stream</button>
  </form>
  <div class="empty-state">Open an existing feed or create a new one.</div>"""
    selected_detail = ""
    if active:
        selected_detail = f"""
  <form method="post" action="/streams/update" class="row">
    <input type="hidden" name="stream_id" value="{html.escape(state.selected_stream_id)}">
    <label>Selected name <input name="name" value="{html.escape(active.name)}"></label>
    <label>Protocol <select name="protocol">{protocol_options}</select></label>
    <label>Mode <select name="mode">{options}</select></label>
    <button type="submit">Update stream</button>
  </form>
  <form method="post" action="/streams/delete" class="row">
    <input type="hidden" name="stream_id" value="{html.escape(state.selected_stream_id)}">
    <button class="secondary" type="submit">Delete selected stream</button>
  </form>
  <div>Status: <strong>{status}</strong></div>
  <div>Intentional outage: <strong>{outage}</strong></div>
  <div>Last error: <strong>{last_error}</strong></div>
  <label>Endpoint<br><input id="endpoint" value="{endpoint}" readonly></label>
  <form method="post" action="/start" class="row">
    <input type="hidden" name="stream_id" value="{html.escape(state.selected_stream_id)}">
    <label>Protocol <select name="protocol">{protocol_options}</select></label>
    <label>Mode <select name="mode">{options}</select></label>
    <button type="submit">Start</button>
  </form>
  <form method="post" action="/start">
    <fieldset>
      <legend>Runtime fault controls</legend>
      <input type="hidden" name="{CONTROL_FORM_FIELD}" value="1">
      <input type="hidden" name="protocol" value="{html.escape(state.protocol)}">
      <input type="hidden" name="stream_id" value="{html.escape(state.selected_stream_id)}">
      <div class="row">
        <label class="control"><input type="checkbox" name="video"{checked["video"]}> Video</label>
        <label class="control"><input type="checkbox" name="audio"{checked["audio"]}> Audio</label>
        <label class="control"><input type="checkbox" name="captions"{checked["captions"]}> Captions</label>
        <label class="control"><input type="checkbox" name="black_video"{checked["black_video"]}> Black video</label>
        <label class="control"><input type="checkbox" name="frozen_video"{checked["frozen_video"]}> Frozen video</label>
        <button type="submit">Apply controls</button>
      </div>
    </fieldset>
  </form>
  <div class="row">
    <form method="post" action="/stop"><input type="hidden" name="stream_id" value="{html.escape(state.selected_stream_id)}"><button type="submit">Stop</button></form>
    <form method="post" action="/validate"><input type="hidden" name="stream_id" value="{html.escape(state.selected_stream_id)}"><button type="submit">Validate</button></form>
    <button type="button" onclick="navigator.clipboard.writeText(document.getElementById('endpoint').value)">Copy URL</button>
    <a href="/diagnostics.txt">Download diagnostics</a>
  </div>
  <h2>Validation</h2>
  <pre>{validation}</pre>
  {preview}
  <h2>Logs</h2>
  <pre>{logs}</pre>"""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Video Feed Simulator</title>
  <link rel="stylesheet" href="/static/app.css">
  <style>
    body {{ font-family: Inter, ui-sans-serif, system-ui, sans-serif; margin: 0; background: #050505; color: #f5f5f1; }}
    main {{ display: grid; gap: 1rem; max-width: 72rem; margin: 0 auto; padding: 1.25rem; }}
    button, .button {{ border: 0; border-radius: 0.45rem; background: #b81d24; color: white; padding: 0.7rem 0.95rem; text-decoration: none; font-weight: 700; }}
    button.secondary, .button.secondary {{ background: #2b2b2b; color: #f5f5f1; }}
    #endpoint {{ width: min(100%, 34rem); padding: 0.65rem; border: 1px solid #2c2c2c; border-radius: 0.45rem; background: #080808; color: #f5f5f1; }}
    fieldset {{ border: 1px solid #2c2c2c; border-radius: 0.5rem; padding: 0.75rem; }}
    label.control {{ display: inline-flex; gap: 0.35rem; align-items: center; margin-right: 0.75rem; }}
    pre {{ background: #050505; color: #f5f5f1; min-height: 12rem; padding: 1rem; overflow: auto; border-radius: 0.5rem; }}
    .row {{ display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: center; }}
    .shell {{ background: #141414; border: 1px solid #2c2c2c; border-radius: 0.5rem; padding: 1rem; box-shadow: 0 1rem 2.5rem rgba(0, 0, 0, 0.4); }}
  </style>
</head>
<body>
<div id="app"></div>
<script id="initial-state" type="application/json">{script_json(state_payload(state))}</script>
<main>
  <div id="app-fallback" class="shell">
  <h1>Video Feed Simulator</h1>
  <h2>Streams</h2>
  <ul>{stream_rows}</ul>
  {selected_detail if active else create_form}
  </div>
</main>
<script type="module" src="/static/app.js"></script>
</body>
</html>"""


def state_payload(state: GuiState) -> dict:
    active = state.active_stream
    controls = controls_for_mode(active.mode if active else state.mode)
    return {
        "selectedStreamId": state.selected_stream_id,
        "feedListUrl": "/",
        "streams": [stream_payload(stream) for stream in state.streams.values()],
        "status": state.status,
        "protocol": state.protocol,
        "protocols": [{"value": value, "label": label} for value, label in PROTOCOL_OPTIONS.items()],
        "mode": state.mode,
        "intentionalOutage": state.intentional_outage,
        "endpoint": state.endpoint,
        "lastError": state.last_error or (active.last_error if active else "") or "none",
        "validationOutput": state.validation_output or (active.validation_output if active else "") or "not run",
        "previewAvailable": bool(active) and state.status == "running" and controls["video"],
        "previewUrl": "/preview.jpg",
        "logs": (state.logs or (active.logs if active else []))[-80:],
        "modes": [
            {"value": mode, "label": label, "controls": MODE_CONTROLS[mode]}
            for mode, (label, _) in PROFILE_OPTIONS.items()
        ],
        "controls": controls,
    }


def stream_payload(stream: FeedRecord) -> dict:
    return {
        "id": stream.id,
        "name": stream.name,
        "url": feed_path(stream.id),
        "protocol": stream.protocol,
        "mode": stream.mode,
        "status": stream.status,
        "endpoint": stream.endpoint,
        "intentionalOutage": stream.intentional_outage,
        "lastError": stream.last_error or "none",
        "validationOutput": stream.validation_output or "not run",
        "logs": stream.logs[-80:],
    }


def script_json(payload) -> str:
    return json.dumps(payload).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def preview_image(state: GuiState) -> tuple[bytes, str]:
    active = state.active_stream
    if not active:
        return preview_placeholder("Create a feed"), "image/svg+xml"
    controls = controls_for_mode(active.mode)
    if state.status != "running":
        return preview_placeholder("Feed stopped"), "image/svg+xml"
    if not controls["video"]:
        return preview_placeholder("No video track"), "image/svg+xml"

    preview_width = min(640, state.width)
    preview_height = max(1, round(state.height * preview_width / state.width))
    pattern = "black" if controls["black_video"] else "smpte"
    overlay = [] if controls["frozen_video"] else ["!", "clockoverlay", "halignment=right", "valignment=top", "shaded-background=true"]
    tmp = tempfile.NamedTemporaryFile(prefix="videosim-preview-", suffix=".rgb", delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        result = subprocess.run(
            [
                "gst-launch-1.0",
                "-q",
                "videotestsrc",
                "num-buffers=1",
                f"pattern={pattern}",
                "!",
                f"video/x-raw,width={state.width},height={state.height},framerate={state.framerate}/1",
            ]
            + overlay
            + [
                "!",
                "videoscale",
                "!",
                "videoconvert",
                "!",
                f"video/x-raw,format=RGB,width={preview_width},height={preview_height}",
                "!",
                "filesink",
                f"location={tmp_path}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=12,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        Path(tmp_path).unlink(missing_ok=True)
        return preview_placeholder(f"Preview unavailable: {exc}"), "image/svg+xml"

    frame_size = preview_width * preview_height * 3
    frame = Path(tmp_path).read_bytes() if Path(tmp_path).exists() else b""
    Path(tmp_path).unlink(missing_ok=True)
    if result.returncode != 0 or len(frame) < frame_size:
        detail = result.stderr.decode("utf-8", "replace").splitlines()[-1:] or ["Preview unavailable"]
        return preview_placeholder(detail[0]), "image/svg+xml"
    return rgb_frame_to_bmp(frame[:frame_size], preview_width, preview_height), "image/bmp"


def rgb_frame_to_bmp(frame: bytes, width: int, height: int) -> bytes:
    row_padding = (4 - (width * 3) % 4) % 4
    pixel_bytes = bytearray()
    for y in range(height - 1, -1, -1):
        row = frame[y * width * 3 : (y + 1) * width * 3]
        for index in range(0, len(row), 3):
            red, green, blue = row[index : index + 3]
            pixel_bytes.extend((blue, green, red))
        pixel_bytes.extend(b"\x00" * row_padding)
    file_size = 54 + len(pixel_bytes)
    header = bytearray(b"BM")
    header.extend(file_size.to_bytes(4, "little"))
    header.extend((0).to_bytes(4, "little"))
    header.extend((54).to_bytes(4, "little"))
    header.extend((40).to_bytes(4, "little"))
    header.extend(width.to_bytes(4, "little"))
    header.extend(height.to_bytes(4, "little"))
    header.extend((1).to_bytes(2, "little"))
    header.extend((24).to_bytes(2, "little"))
    header.extend((0).to_bytes(4, "little"))
    header.extend(len(pixel_bytes).to_bytes(4, "little"))
    header.extend((2835).to_bytes(4, "little"))
    header.extend((2835).to_bytes(4, "little"))
    header.extend((0).to_bytes(4, "little"))
    header.extend((0).to_bytes(4, "little"))
    return bytes(header + pixel_bytes)


def preview_placeholder(message: str) -> bytes:
    safe = html.escape(message)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360" viewBox="0 0 640 360">
  <rect width="640" height="360" fill="#101820"/>
  <rect x="18" y="18" width="604" height="324" rx="8" fill="none" stroke="#314455"/>
  <text x="320" y="176" fill="#eef6f9" font-family="system-ui, sans-serif" font-size="24" font-weight="700" text-anchor="middle">Feed Preview</text>
  <text x="320" y="212" fill="#9fb3c1" font-family="system-ui, sans-serif" font-size="16" text-anchor="middle">{safe}</text>
</svg>""".encode("utf-8")


def diagnostics_text(state: GuiState) -> str:
    active = state.active_stream
    logs_source = state.logs or (active.logs if active else [])
    return "\n".join(
        [
            "Video Feed Simulator diagnostics",
            f"selected_stream={state.selected_stream_id}",
            f"status={state.status}",
            f"protocol={state.protocol}",
            f"mode={state.mode}",
            f"intentional_outage={'yes' if state.intentional_outage else 'no'}",
            f"endpoint={state.endpoint}",
            f"last_error={state.last_error or 'none'}",
            "",
            "validation:",
            state.validation_output or (active.validation_output if active else "") or "not run",
            "",
            "logs:",
            *logs_source[-200:],
            "",
            "streams:",
            *[
                f"{stream.id} name={stream.name} protocol={stream.protocol} mode={stream.mode} status={stream.status} endpoint={stream.endpoint}"
                for stream in state.streams.values()
            ],
            "",
        ]
    )


def run_gui(host: str, port: int, state: GuiState):
    state.http_port = port
    for stream in state.streams.values():
        stream.http_port = port
    state._sync_from_active()
    handler = type("VideoSimGuiHandler", (GuiHandler,), {"state": state})
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Video Feed Simulator GUI: http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    finally:
        state.stop_all()
        server.server_close()
