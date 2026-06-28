from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import html
import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from urllib.parse import parse_qs


PROFILE_OPTIONS = {
    "normal": ("Normal", "profiles/srt-normal.yaml"),
    "audio_only": ("Audio only", "profiles/srt-audio-only.yaml"),
    "video_only": ("Video only", "profiles/srt-video-only.yaml"),
    "no_captions": ("No captions", "profiles/srt-no-captions.yaml"),
    "black_video": ("Black video", "profiles/srt-black-video.yaml"),
    "frozen_video": ("Frozen video", "profiles/srt-frozen-video.yaml"),
}

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


@dataclass
class GuiState:
    feed_port: int = 9000
    width: int = 1280
    height: int = 720
    framerate: int = 30
    mode: str = "normal"
    process: subprocess.Popen | None = None
    logs: list[str] = field(default_factory=list)
    last_error: str = ""
    validation_output: str = ""

    @property
    def endpoint(self) -> str:
        return f"srt://127.0.0.1:{self.feed_port}?mode=caller"

    @property
    def status(self) -> str:
        if self.process and self.process.poll() is None:
            return "running"
        return "stopped"

    @property
    def intentional_outage(self) -> bool:
        return self.mode != "normal"

    def start(self):
        if self.status == "running":
            self.log("Feed already running")
            return False
        if self.mode not in PROFILE_OPTIONS:
            self.fail(f"Unsupported mode: {self.mode}")
            return False
        _, profile = PROFILE_OPTIONS[self.mode]
        cmd = [
            sys.executable,
            "-m",
            "videosim",
            "start",
            "--profile",
            profile,
            "--port",
            str(self.feed_port),
            "--width",
            str(self.width),
            "--height",
            str(self.height),
            "--framerate",
            str(self.framerate),
        ]
        self.last_error = ""
        try:
            self.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        except OSError as exc:
            self.process = None
            self.fail(f"Failed to start {self.mode} feed: {exc}")
            return False
        self.log(f"Started {self.mode} feed at {self.endpoint}")
        threading.Thread(target=self._capture_logs, args=(self.process,), daemon=True).start()
        return True

    def apply_mode(self, mode: str):
        if mode not in PROFILE_OPTIONS:
            self.fail(f"Unsupported mode: {mode}")
            return False
        if self.status == "running" and mode == self.mode:
            self.log("Feed already running")
            return True
        if self.status == "running":
            self.log(f"Restarting feed for {mode} mode")
            self.stop()
        self.mode = mode
        return self.start()

    def validate(self):
        if self.mode not in PROFILE_OPTIONS:
            self.fail(f"Unsupported mode: {self.mode}")
            return False
        _, profile = PROFILE_OPTIONS[self.mode]
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "videosim",
                    "validate",
                    "--profile",
                    profile,
                    "--port",
                    str(self.feed_port),
                    "--width",
                    str(self.width),
                    "--height",
                    str(self.height),
                    "--framerate",
                    str(self.framerate),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=45,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.validation_output = ""
            self.fail(f"Validation failed: {exc}")
            return False
        self.validation_output = result.stdout.strip()
        self.log("Validation passed" if result.returncode == 0 else "Validation failed")
        if result.returncode != 0:
            self.last_error = self.validation_output.splitlines()[-1] if self.validation_output else "Validation failed"
        return result.returncode == 0

    def stop(self):
        if not self.process or self.process.poll() is not None:
            self.process = None
            self.log("Feed already stopped")
            return
        process = self.process
        self.log("Stopping feed")
        os.killpg(process.pid, signal.SIGINT)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            self.log("Killed stuck feed")
        self.process = None
        self.log("Feed stopped")

    def fail(self, message: str):
        self.last_error = message
        self.log(message)

    def log(self, message: str):
        self.logs.append(message)
        del self.logs[:-200]

    def _capture_logs(self, process):
        if not process.stdout:
            return
        last_line = ""
        for line in process.stdout:
            last_line = line.rstrip()
            self.log(last_line)
        code = process.poll()
        if code not in (None, 0):
            detail = f": {last_line}" if last_line else ""
            self.fail(f"Feed process exited with code {code}{detail}")


class GuiHandler(BaseHTTPRequestHandler):
    state: GuiState

    def do_GET(self):
        if self.path == "/diagnostics.txt":
            self._send_text(diagnostics_text(self.state))
            return
        if self.path != "/":
            self.send_error(404)
            return
        self._send_html(render_page(self.state))

    def do_POST(self):
        if self.path == "/start":
            params = self._read_form()
            try:
                mode = mode_from_form(params, self.state.mode)
            except ValueError as exc:
                self.state.log(str(exc))
                self._redirect_home()
                return
            self.state.apply_mode(mode)
        elif self.path == "/stop":
            self.state.stop()
        elif self.path == "/validate":
            self.state.validate()
        else:
            self.send_error(404)
            return
        self._redirect_home()

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

    def _redirect_home(self):
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def _read_form(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8") if length else ""
        return parse_qs(body)


def render_page(state: GuiState) -> str:
    logs = "\n".join(html.escape(line) for line in state.logs[-80:])
    status = html.escape(state.status)
    endpoint = html.escape(state.endpoint)
    outage = "yes" if state.intentional_outage else "no"
    last_error = html.escape(state.last_error or "none")
    validation = html.escape(state.validation_output or "not run")
    options = "\n".join(
        f'<option value="{html.escape(mode)}"{" selected" if mode == state.mode else ""}>{html.escape(label)}</option>'
        for mode, (label, _) in PROFILE_OPTIONS.items()
    )
    controls = controls_for_mode(state.mode)
    checked = {field: " checked" if controls[field] else "" for field in CONTROL_FIELDS}
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Video Feed Simulator</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem; max-width: 56rem; }}
    main {{ display: grid; gap: 1rem; }}
    button {{ padding: 0.6rem 0.9rem; }}
    #endpoint {{ width: min(100%, 32rem); padding: 0.55rem; }}
    fieldset {{ border: 1px solid #bbb; padding: 0.75rem; }}
    label.control {{ display: inline-flex; gap: 0.35rem; align-items: center; margin-right: 0.75rem; }}
    pre {{ background: #111; color: #eee; min-height: 12rem; padding: 1rem; overflow: auto; }}
    .row {{ display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: center; }}
  </style>
</head>
<body>
<main>
  <h1>Video Feed Simulator</h1>
  <div>Status: <strong>{status}</strong></div>
  <div>Intentional outage: <strong>{outage}</strong></div>
  <div>Last error: <strong>{last_error}</strong></div>
  <label>SRT endpoint<br><input id="endpoint" value="{endpoint}" readonly></label>
  <form method="post" action="/start" class="row">
    <label>Mode <select name="mode">{options}</select></label>
    <button type="submit">Start</button>
  </form>
  <form method="post" action="/start">
    <fieldset>
      <legend>Runtime fault controls</legend>
      <input type="hidden" name="{CONTROL_FORM_FIELD}" value="1">
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
    <form method="post" action="/stop"><button type="submit">Stop</button></form>
    <form method="post" action="/validate"><button type="submit">Validate</button></form>
    <button type="button" onclick="navigator.clipboard.writeText(document.getElementById('endpoint').value)">Copy URL</button>
    <a href="/diagnostics.txt">Download diagnostics</a>
  </div>
  <h2>Validation</h2>
  <pre>{validation}</pre>
  <h2>Logs</h2>
  <pre>{logs}</pre>
</main>
</body>
</html>"""


def diagnostics_text(state: GuiState) -> str:
    return "\n".join(
        [
            "Video Feed Simulator diagnostics",
            f"status={state.status}",
            f"mode={state.mode}",
            f"intentional_outage={'yes' if state.intentional_outage else 'no'}",
            f"endpoint={state.endpoint}",
            f"last_error={state.last_error or 'none'}",
            "",
            "validation:",
            state.validation_output or "not run",
            "",
            "logs:",
            *state.logs[-200:],
            "",
        ]
    )


def run_gui(host: str, port: int, state: GuiState):
    handler = type("VideoSimGuiHandler", (GuiHandler,), {"state": state})
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Video Feed Simulator GUI: http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    finally:
        state.stop()
        server.server_close()
