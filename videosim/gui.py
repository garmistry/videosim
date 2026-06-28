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


@dataclass
class GuiState:
    feed_port: int = 9000
    width: int = 1280
    height: int = 720
    framerate: int = 30
    mode: str = "normal"
    process: subprocess.Popen | None = None
    logs: list[str] = field(default_factory=list)

    @property
    def endpoint(self) -> str:
        return f"srt://127.0.0.1:{self.feed_port}?mode=caller"

    @property
    def status(self) -> str:
        if self.process and self.process.poll() is None:
            return "running"
        return "stopped"

    def start(self):
        if self.status == "running":
            self.log("Feed already running")
            return
        if self.mode not in PROFILE_OPTIONS:
            self.log(f"Unsupported mode: {self.mode}")
            return
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
        self.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        self.log(f"Started {self.mode} feed at {self.endpoint}")
        threading.Thread(target=self._capture_logs, args=(self.process,), daemon=True).start()

    def stop(self):
        if not self.process or self.process.poll() is not None:
            self.log("Feed already stopped")
            return
        self.log("Stopping feed")
        os.killpg(self.process.pid, signal.SIGINT)
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=5)
            self.log("Killed stuck feed")
        self.log("Feed stopped")

    def log(self, message: str):
        self.logs.append(message)
        del self.logs[:-200]

    def _capture_logs(self, process):
        if not process.stdout:
            return
        for line in process.stdout:
            self.log(line.rstrip())


class GuiHandler(BaseHTTPRequestHandler):
    state: GuiState

    def do_GET(self):
        if self.path != "/":
            self.send_error(404)
            return
        self._send_html(render_page(self.state))

    def do_POST(self):
        if self.path == "/start":
            params = self._read_form()
            mode = params.get("mode", [self.state.mode])[0]
            if mode in PROFILE_OPTIONS:
                self.state.mode = mode
            else:
                self.state.log(f"Unsupported mode: {mode}")
                self._redirect_home()
                return
            self.state.start()
        elif self.path == "/stop":
            self.state.stop()
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
    options = "\n".join(
        f'<option value="{html.escape(mode)}"{" selected" if mode == state.mode else ""}>{html.escape(label)}</option>'
        for mode, (label, _) in PROFILE_OPTIONS.items()
    )
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
    input {{ width: min(100%, 32rem); padding: 0.55rem; }}
    pre {{ background: #111; color: #eee; min-height: 12rem; padding: 1rem; overflow: auto; }}
    .row {{ display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: center; }}
  </style>
</head>
<body>
<main>
  <h1>Video Feed Simulator</h1>
  <div>Status: <strong>{status}</strong></div>
  <label>SRT endpoint<br><input id="endpoint" value="{endpoint}" readonly></label>
  <form method="post" action="/start" class="row">
    <label>Mode <select name="mode">{options}</select></label>
    <button type="submit">Start</button>
  </form>
  <div class="row">
    <form method="post" action="/stop"><button type="submit">Stop</button></form>
    <button type="button" onclick="navigator.clipboard.writeText(document.getElementById('endpoint').value)">Copy URL</button>
  </div>
  <h2>Logs</h2>
  <pre>{logs}</pre>
</main>
</body>
</html>"""


def run_gui(host: str, port: int, state: GuiState):
    handler = type("VideoSimGuiHandler", (GuiHandler,), {"state": state})
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Video Feed Simulator GUI: http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    finally:
        state.stop()
        server.server_close()
