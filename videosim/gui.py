from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import copy
import html
import json
import math
import mimetypes
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, unquote, urlparse

from .alert_profile import alert_profile_payload, normalize_alert_delay, normalize_enabled_alerts
from .control_plane import (
    MAX_IDENTIFIER_LENGTH,
    MAX_REPORT_STREAMS,
    WORKER_API_VERSION,
    WORKER_API_VERSION_V2,
    AssignmentContract,
    WorkerReportConflict,
    WorkerReportPersistenceError,
    WorkerReportValidationError,
    assignment_fingerprint,
    assignment_token,
    validate_monitor_items,
    validate_report_contract,
)
from .feed_store import FeedRegistrationStore
from .framerate import frame_rate_float, frame_rate_fraction, normalize_frame_rate, supported_frame_rate_options
from .monitor_catalog import monitor_catalog_payload
from .profile import load_profile
from .postgres_store import (
    CheckResult,
    FencedReport,
    LeaseConflict,
    PostgresControlPlaneStore,
    PostgresStoreError,
    ReportConflict,
)
from .security import AuthenticationError, AuthorizationError, EndpointPolicyError, Principal, SecurityConfig, audit_event
from .validator import human_summary, validate_config


PROFILE_OPTIONS = {
    "normal": ("Normal", "profiles/srt-normal.yaml"),
    "audio_only": ("Audio only", "profiles/srt-audio-only.yaml"),
    "video_only": ("Video only", "profiles/srt-video-only.yaml"),
    "no_captions": ("No captions", "profiles/srt-no-captions.yaml"),
    "black_video": ("Black video", "profiles/srt-black-video.yaml"),
    "frozen_video": ("Frozen video", "profiles/srt-frozen-video.yaml"),
}
MAX_WORKER_STREAMS = 100_000
PROTOCOL_CAPACITY_FIELDS = {
    "srt": "maxSrtStreams",
    "dash": "maxDashStreams",
}

PROTOCOL_OPTIONS = {"srt": "SRT", "dash": "DASH"}
SOURCE_OPTIONS = {"generated": "Generated", "external": "External URL"}

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
DEFAULT_MONITOR_STATE_PATH = "/tmp/videosim-monitor/state.json"
WORKER_TTL_SECONDS = 60
MAX_REQUEST_BODY_BYTES = 1024 * 1024


class OperatorMutationPersistenceError(RuntimeError):
    """A persistent operator mutation could not commit with its audit row."""


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


def format_bitrate(bits_per_second: int) -> str:
    if bits_per_second >= 1_000_000:
        return f"{bits_per_second / 1_000_000:.1f} Mbps"
    if bits_per_second >= 1_000:
        return f"{bits_per_second / 1_000:.0f} kbps"
    return f"{bits_per_second} bps"


def format_bytes(byte_count: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(byte_count)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{byte_count} B"


def normalize_source(source: str) -> str:
    source = (source or "generated").strip()
    if source not in SOURCE_OPTIONS:
        raise ValueError(f"Unsupported feed source: {source}")
    return source


def validate_worker_base_url(url: str, security: SecurityConfig) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("worker base URL must be an http(s) origin without credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("worker base URL must not contain a path, query, or fragment")
    if security.enabled and parsed.scheme != "https":
        raise ValueError("trusted-proxy worker base URL must use https")
    return url


def validate_external_url(protocol: str, url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ValueError("External feed URL is required")
    parsed = urlparse(url)
    if protocol == "srt":
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("External SRT URL must include a valid port") from exc
        if parsed.scheme != "srt" or not parsed.hostname or not port:
            raise ValueError("External SRT URL must look like srt://host:port?mode=caller")
        return url
    if protocol == "dash":
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("External DASH URL must be an http(s) manifest URL")
        return url
    raise ValueError(f"Unsupported protocol: {protocol}")


@dataclass
class FeedRecord:
    id: str
    name: str
    protocol: str
    http_port: int
    feed_port: int
    width: int
    height: int
    framerate: str | int | float
    dash_dir: str
    mode: str
    source: str = "generated"
    external_url: str = ""
    process: subprocess.Popen | None = None
    logs: list[str] = field(default_factory=list)
    last_error: str = ""
    validation_output: str = ""
    started_at: float | None = None
    last_run_seconds: float = 0
    last_outbound_bytes: int = 0
    alert_enabled_ids: list[str] | None = None
    alert_delay_seconds: int = 0
    # PostgreSQL-only optimistic fence. It is not part of persisted config.
    config_version: int = 0

    def __post_init__(self):
        self.framerate = normalize_frame_rate(self.framerate)
        self.source = normalize_source(self.source)
        self.external_url = validate_external_url(self.protocol, self.external_url) if self.source == "external" else ""
        self.alert_enabled_ids = normalize_enabled_alerts(self.alert_enabled_ids)
        self.alert_delay_seconds = normalize_alert_delay(self.alert_delay_seconds)
        if not isinstance(self.config_version, int) or self.config_version < 0:
            raise ValueError("config_version must be a non-negative integer")

    @property
    def endpoint(self) -> str:
        if self.source == "external":
            return self.external_url
        if self.protocol == "dash":
            return f"http://127.0.0.1:{self.http_port}/dash/{self.id}/manifest.mpd"
        return f"srt://127.0.0.1:{self.feed_port}?mode=caller"

    @property
    def status(self) -> str:
        if self.source == "external":
            return "running"
        if self.process and self.process.poll() is None:
            return "running"
        return "stopped"

    @property
    def intentional_outage(self) -> bool:
        return self.source == "generated" and self.mode != "normal"

    def bitrate_bps(self) -> int:
        if self.source == "external":
            return 0
        controls = controls_for_mode(self.mode)
        video_bps = 0
        if controls["video"]:
            scale = (self.width * self.height * frame_rate_float(self.framerate)) / (1280 * 720 * 30)
            video_bps = int(4_000_000 * scale)
            if controls["black_video"] or controls["frozen_video"]:
                video_bps = int(video_bps * 0.35)
        audio_bps = 128_000 if controls["audio"] else 0
        caption_bps = 8_000 if controls["captions"] else 0
        return int((video_bps + audio_bps + caption_bps) * 1.05)

    def metrics(self, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else now
        if self.source == "external":
            return {
                "uptimeSeconds": 0,
                "bitrateBps": 0,
                "bitrateLabel": format_bitrate(0),
                "outboundBytes": 0,
                "outboundLabel": format_bytes(0),
                "videoFrames": 0,
                "videoFramesLabel": "0",
            }
        running = self.status == "running"
        uptime = max(0.0, now - self.started_at) if running and self.started_at is not None else self.last_run_seconds
        bitrate_bps = self.bitrate_bps() if running else 0
        outbound_bytes = int(bitrate_bps * uptime / 8) if running else self.last_outbound_bytes
        video_frames = int(uptime * frame_rate_float(self.framerate)) if controls_for_mode(self.mode)["video"] else 0
        return {
            "uptimeSeconds": round(uptime, 1),
            "bitrateBps": bitrate_bps,
            "bitrateLabel": format_bitrate(bitrate_bps),
            "outboundBytes": outbound_bytes,
            "outboundLabel": format_bytes(outbound_bytes),
            "videoFrames": video_frames,
            "videoFramesLabel": f"{video_frames:,}",
        }

    def finish_run(self):
        if self.started_at is not None:
            self.last_run_seconds = round(max(0.0, time.monotonic() - self.started_at), 1)
            self.last_outbound_bytes = int(self.bitrate_bps() * self.last_run_seconds / 8)
        self.started_at = None


@dataclass
class GuiState:
    protocol: str = "srt"
    http_port: int = 8080
    feed_port: int = 9000
    width: int = 1280
    height: int = 720
    framerate: str | int | float = "59.94"
    dash_dir: str = "/tmp/videosim-dash"
    monitor_state_path: str = ""
    worker_base_url: str = ""
    mode: str = "normal"
    process: subprocess.Popen | None = None
    logs: list[str] = field(default_factory=list)
    last_error: str = ""
    validation_output: str = ""
    streams: dict[str, FeedRecord] = field(default_factory=dict)
    selected_stream_id: str = ""
    feed_store: FeedRegistrationStore | None = None
    worker_seen: dict[str, float] = field(default_factory=dict)
    monitor_state_lock: threading.RLock = field(default_factory=threading.RLock)
    control_plane_lock: threading.RLock = field(default_factory=threading.RLock)
    control_plane_instance_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    assignment_generation: int = 1
    assignment_fingerprint: str = ""
    assignment_dirty: bool = True
    allow_legacy_worker_reports: bool = False
    deleting_stream_ids: set[str] = field(default_factory=set)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    _next_stream_number: int = 1

    def __post_init__(self):
        self.framerate = normalize_frame_rate(self.framerate)
        self.worker_base_url = validate_worker_base_url(self.worker_base_url, self.security)
        if self.feed_store and not self.streams:
            self._load_streams_from_store()
        for stream in list(self.streams.values()):
            if stream.source == "external":
                self.security.validate_external_endpoint(stream.external_url)
        if self.streams:
            self._next_stream_number = max(self._next_stream_number, next_stream_number(self.streams))
        if self.streams and not self.selected_stream_id:
            self.select_stream(next(iter(self.streams)))

    @property
    def active_stream(self) -> FeedRecord | None:
        with self.control_plane_lock:
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
        source: str = "generated",
        external_url: str = "",
        feed_port: int | None = None,
        width: int | None = None,
        height: int | None = None,
        framerate: str | int | float | None = None,
        select: bool = True,
        audit: dict | None = None,
    ) -> FeedRecord:
        if protocol not in PROTOCOL_OPTIONS:
            raise ValueError(f"Unsupported protocol: {protocol}")
        if mode not in PROFILE_OPTIONS:
            raise ValueError(f"Unsupported mode: {mode}")
        source = normalize_source(source)
        with self.control_plane_lock:
            stream_number = self._next_stream_number
            stream_id = f"stream-{stream_number}"
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
                source=source,
                external_url=external_url,
                alert_enabled_ids=[] if source == "external" else None,
            )
            if stream.source == "external":
                self.security.validate_external_endpoint(stream.external_url)
            self._persist_stream(stream, audit=audit)
            self.streams[stream_id] = stream
            self._next_stream_number = stream_number + 1
            mark_assignment_dirty(self)
        if select:
            self.select_stream(stream_id)
        return stream

    def next_available_port(self) -> int:
        with self.control_plane_lock:
            used = {stream.feed_port for stream in self.streams.values()}
            port = self.feed_port
            while port in used:
                port += 1
            return port

    def select_stream(self, stream_id: str) -> bool:
        with self.control_plane_lock:
            if stream_id not in self.streams:
                self.fail(f"Unsupported stream: {stream_id}")
                return False
            self.selected_stream_id = stream_id
            self._sync_from_active()
            return True

    def clear_selection(self):
        self.selected_stream_id = ""
        self._sync_from_active()

    def _ensure_current_stream_locked(self, stream: FeedRecord, operation: str) -> bool:
        """Reject a stale mutation after a concurrent delete has started."""
        if (
            self.streams.get(stream.id) is not stream
            or stream.id in self.deleting_stream_ids
        ):
            self.fail(f"Stream changed during {operation}: {stream.id}", stream.id)
            return False
        return True

    def update_stream(
        self,
        stream_id: str,
        name: str | None = None,
        protocol: str | None = None,
        mode: str | None = None,
        source: str | None = None,
        external_url: str | None = None,
        framerate: str | int | float | None = None,
        audit: dict | None = None,
    ) -> bool:
        with self.control_plane_lock:
            stream = self.streams.get(stream_id)
        if stream is None:
            self.fail(f"Unsupported stream: {stream_id}")
            return False
        next_protocol = protocol if protocol is not None else stream.protocol
        next_mode = mode if mode is not None else stream.mode
        next_source = normalize_source(source if source is not None else stream.source)
        next_framerate = normalize_frame_rate(framerate) if framerate is not None else stream.framerate
        next_external_url = external_url if external_url is not None else stream.external_url
        if next_protocol not in PROTOCOL_OPTIONS:
            self.fail(f"Unsupported protocol: {next_protocol}")
            return False
        if next_mode not in PROFILE_OPTIONS:
            self.fail(f"Unsupported mode: {next_mode}")
            return False
        next_external_url = validate_external_url(next_protocol, next_external_url) if next_source == "external" else ""
        if next_source == "external":
            self.security.validate_external_endpoint(next_external_url)
        restart = stream.source == "generated" and stream.status == "running"
        with self.control_plane_lock:
            if not self._ensure_current_stream_locked(stream, "update"):
                return False
            candidate = copy.copy(stream)
            if name is not None and name.strip():
                candidate.name = name.strip()
            candidate.protocol = next_protocol
            candidate.mode = next_mode
            if candidate.source != next_source:
                candidate.alert_enabled_ids = [] if next_source == "external" else None
            candidate.source = next_source
            candidate.external_url = next_external_url
            candidate.framerate = next_framerate
            self._persist_stream(candidate, audit=audit)
            for field_name in (
                "name",
                "protocol",
                "mode",
                "source",
                "external_url",
                "framerate",
                "alert_enabled_ids",
                "config_version",
            ):
                setattr(stream, field_name, getattr(candidate, field_name))
            mark_assignment_dirty(self)
        self.select_stream(stream_id)
        if restart:
            self.log("Stopping feed after committed configuration update", stream.id)
            if not self._terminate_stream_process(stream):
                self.fail(
                    "Configuration committed, but the previous process could not be stopped",
                    stream.id,
                )
                return False
            with self.control_plane_lock:
                mark_assignment_dirty(self)
            self._sync_from_active()
            self.log("Previous feed process stopped", stream.id)
        if restart and stream.source == "generated":
            return self.start(stream_id)
        return True

    def update_alert_profile(
        self,
        stream_id: str,
        enabled_ids: list[str] | None,
        delay_seconds,
        audit: dict | None = None,
    ) -> bool:
        with self.control_plane_lock:
            stream = self.streams.get(stream_id)
        if stream is None:
            self.fail(f"Unsupported stream: {stream_id}")
            return False
        try:
            with self.control_plane_lock:
                if not self._ensure_current_stream_locked(stream, "alert profile update"):
                    return False
                candidate = copy.copy(stream)
                candidate.alert_enabled_ids = normalize_enabled_alerts(enabled_ids)
                candidate.alert_delay_seconds = normalize_alert_delay(delay_seconds)
                self._persist_stream(candidate, audit=audit)
                stream.alert_enabled_ids = candidate.alert_enabled_ids
                stream.alert_delay_seconds = candidate.alert_delay_seconds
                stream.config_version = candidate.config_version
                mark_assignment_dirty(self)
        except ValueError as exc:
            self.fail(str(exc), stream_id)
            return False
        self.select_stream(stream_id)
        return True

    def delete_stream(
        self, stream_id: str, audit: dict | None = None
    ) -> bool:
        with self.control_plane_lock:
            if stream_id not in self.streams:
                self.fail(f"Unsupported stream: {stream_id}")
                return False
            if stream_id in self.deleting_stream_ids:
                return False
            self.deleting_stream_ids.add(stream_id)
        stream = None
        try:
            with self.control_plane_lock:
                stream = self.streams.get(stream_id)
                if stream is None:
                    return False
                # Commit the persistent delete and immutable audit first. A
                # failed audit transaction must not stop or remove the feed.
                self._delete_persisted_stream(
                    stream_id, audit=audit, expected_version=stream.config_version
                )
                # The committed durable delete is now the control-plane truth;
                # publish it in memory before best-effort local cleanup.
                del self.streams[stream_id]
                mark_assignment_dirty(self)
        except ValueError as exc:
            self.fail(str(exc), stream_id)
            return False
        finally:
            with self.control_plane_lock:
                self.deleting_stream_ids.discard(stream_id)
        if self.selected_stream_id == stream_id:
            self.selected_stream_id = ""
        if self.streams and not self.selected_stream_id:
            self.select_stream(next(iter(self.streams)))
        elif not self.streams:
            self._sync_from_active()
        if (
            stream is not None
            and stream.source == "generated"
            and not self._terminate_stream_process(stream)
        ):
            self.fail(
                f"Feed {stream_id} was deleted, but local process cleanup is retrying"
            )
            threading.Thread(
                target=self._retry_detached_process_cleanup,
                args=(stream,),
                daemon=True,
            ).start()
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
        if stream_id and not self.select_stream(stream_id):
            return False
        stream = self.active_stream
        if not stream:
            self.fail("Create a feed before starting")
            return False
        if stream.source == "external":
            self.log("External feed is registered for monitoring", stream.id)
            return True
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
        self.last_error = ""
        container = "yes" if running_in_container() else "no"
        self.log(
            f"Starting {stream.protocol} {stream.mode} feed: profile={profile} endpoint={stream.endpoint} container={container}",
            stream.id,
        )
        if verbose_enabled():
            self.log(f"Feed launch command: {shlex.join(cmd)}", stream.id)
        with self.control_plane_lock:
            if self.streams.get(stream.id) is not stream or stream.id in self.deleting_stream_ids:
                self.fail(f"Stream changed before start: {stream.id}")
                return False
            if stream.status == "running":
                self.log("Feed already running", stream.id)
                return False
            try:
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                )
            except OSError as exc:
                stream.process = None
                self.fail(f"Failed to start {stream.mode} feed: {exc}", stream.id)
                return False
            stream.process = process
            stream.started_at = time.monotonic()
            stream.last_run_seconds = 0
            stream.last_outbound_bytes = 0
            mark_assignment_dirty(self)
        self._sync_from_active()
        self.log(f"Started {stream.protocol} {stream.mode} feed at {stream.endpoint} pid={process.pid}", stream.id)
        threading.Thread(target=self._capture_logs, args=(stream.id, process), daemon=True).start()
        return True

    def apply_mode(
        self,
        mode: str,
        protocol: str | None = None,
        stream_id: str | None = None,
        audit: dict | None = None,
    ):
        if stream_id and not self.select_stream(stream_id):
            return False
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
        changed = mode != stream.mode or protocol != stream.protocol
        if stream.source == "external":
            if not changed:
                self.log("External feed expectation is unchanged", stream.id)
                return True
            try:
                with self.control_plane_lock:
                    if not self._ensure_current_stream_locked(
                        stream, "mode update"
                    ):
                        return False
                    candidate = copy.copy(stream)
                    candidate.mode = mode
                    candidate.protocol = protocol
                    candidate.external_url = validate_external_url(
                        candidate.protocol, candidate.external_url
                    )
                    self.security.validate_external_endpoint(candidate.external_url)
                    self._persist_stream(candidate, audit=audit)
                    stream.mode = candidate.mode
                    stream.protocol = candidate.protocol
                    stream.external_url = candidate.external_url
                    stream.config_version = candidate.config_version
                    mark_assignment_dirty(self)
            except (AuthorizationError, ValueError) as exc:
                self.fail(str(exc), stream.id)
                return False
            self._sync_from_active()
            self.log(f"Updated external feed expectation to {protocol} {mode}", stream.id)
            return True
        was_running = stream.status == "running"
        if not changed:
            if was_running:
                self.log("Feed already running", stream.id)
                return True
            return self.start(stream.id)
        # Commit generated-feed expectation plus audit before local stop/start.
        with self.control_plane_lock:
            if not self._ensure_current_stream_locked(stream, "mode update"):
                return False
            candidate = copy.copy(stream)
            candidate.mode = mode
            candidate.protocol = protocol
            self._persist_stream(candidate, audit=audit)
            stream.mode = candidate.mode
            stream.protocol = candidate.protocol
            stream.config_version = candidate.config_version
            mark_assignment_dirty(self)
        self._sync_from_active()
        if was_running:
            self.log(f"Restarting feed for {protocol} {mode} mode", stream.id)
            if not self.stop(stream.id):
                self.fail(
                    "Expectation committed, but the previous process could not be stopped",
                    stream.id,
                )
                return False
        return self.start(stream.id)

    def validate(self, stream_id: str | None = None):
        if stream_id and not self.select_stream(stream_id):
            return False
        stream = self.active_stream
        if not stream:
            self.fail("Create a feed before validating")
            return False
        if stream.mode not in PROFILE_OPTIONS:
            self.fail(f"Unsupported mode: {stream.mode}", stream.id)
            return False
        if stream.source == "external":
            try:
                config = external_validation_config(stream)
                report = validate_config(config)
            except Exception as exc:
                stream.validation_output = ""
                self.fail(f"Validation failed: {exc}", stream.id)
                return False
            stream.validation_output = human_summary(report)
            self.log("Validation passed" if report.passed else "Validation failed", stream.id)
            if not report.passed:
                stream.last_error = stream.validation_output.splitlines()[-1] if stream.validation_output else "Validation failed"
            self._sync_from_active()
            return report.passed
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

    def _terminate_stream_process(self, stream: FeedRecord) -> bool:
        process = stream.process
        if not process or process.poll() is not None:
            stream.finish_run()
            stream.process = None
            return True
        try:
            os.killpg(process.pid, signal.SIGINT)
        except ProcessLookupError:
            pass
        except OSError as exc:
            self.fail(f"Feed process cleanup failed: {exc}", stream.id)
            return False
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError as exc:
                self.fail(f"Feed process cleanup failed: {exc}", stream.id)
                return False
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.fail(
                    f"Feed process {process.pid} did not exit after SIGKILL",
                    stream.id,
                )
                return False
            self.log("Killed stuck feed", stream.id)
        stream.finish_run()
        stream.process = None
        return True

    def _retry_detached_process_cleanup(self, stream: FeedRecord):
        for attempt in range(1, 4):
            time.sleep(float(attempt))
            if self._terminate_stream_process(stream):
                self.log(
                    f"Detached process cleanup succeeded on retry {attempt} for {stream.id}"
                )
                return
        process_id = stream.process.pid if stream.process else "unknown"
        self.fail(
            f"Detached process cleanup exhausted for {stream.id} pid={process_id}"
        )

    def stop(self, stream_id: str | None = None) -> bool:
        if stream_id and not self.select_stream(stream_id):
            return False
        stream = self.active_stream
        if not stream:
            self.log("No feeds configured")
            return True
        if stream.source == "external":
            self.log("External feed has no local process to stop", stream.id)
            return True
        if not stream.process or stream.process.poll() is not None:
            with self.control_plane_lock:
                stream.finish_run()
                stream.process = None
                mark_assignment_dirty(self)
            self._sync_from_active()
            self.log("Feed already stopped", stream.id)
            return True
        self.log("Stopping feed", stream.id)
        if not self._terminate_stream_process(stream):
            self._sync_from_active()
            return False
        with self.control_plane_lock:
            mark_assignment_dirty(self)
        self._sync_from_active()
        self.log("Feed stopped", stream.id)
        return True

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

    def _load_streams_from_store(self):
        try:
            feeds = self.feed_store.load() if self.feed_store else []
        except Exception as exc:
            self.logs.append(f"Feed DB load failed: {exc}")
            return
        for feed in feeds:
            try:
                stream = FeedRecord(**feed)
                if stream.source == "external":
                    self.security.validate_external_endpoint(stream.external_url)
            except (EndpointPolicyError, TypeError, ValueError) as exc:
                self.logs.append(f"Skipped stored feed {feed.get('id', '<unknown>')}: {exc}")
                continue
            self.streams[stream.id] = stream

    def _persist_stream(self, stream: FeedRecord, audit: dict | None = None):
        if not self.feed_store:
            return
        try:
            if audit is not None and isinstance(
                self.feed_store, PostgresControlPlaneStore
            ):
                stream.config_version = self.feed_store.upsert_with_audit(
                    stream_registration(stream),
                    audit,
                    expected_version=stream.config_version,
                )
            else:
                if isinstance(self.feed_store, PostgresControlPlaneStore):
                    stream.config_version = self.feed_store.upsert_versioned(
                        stream_registration(stream), stream.config_version
                    )
                else:
                    self.feed_store.upsert(stream_registration(stream))
        except Exception as exc:
            if audit is not None and isinstance(
                self.feed_store, PostgresControlPlaneStore
            ):
                raise OperatorMutationPersistenceError(
                    f"Feed mutation/audit transaction failed: {exc}"
                ) from exc
            raise ValueError(f"Feed registration failed: {exc}") from exc

    def _delete_persisted_stream(
        self,
        stream_id: str,
        audit: dict | None = None,
        expected_version: int | None = None,
    ):
        if not self.feed_store:
            return
        try:
            if audit is not None and isinstance(
                self.feed_store, PostgresControlPlaneStore
            ):
                self.feed_store.delete_with_audit(
                    stream_id, audit, expected_version=expected_version
                )
            else:
                if isinstance(self.feed_store, PostgresControlPlaneStore):
                    self.feed_store.delete_versioned(stream_id, expected_version)
                else:
                    self.feed_store.delete(stream_id)
        except Exception as exc:
            if audit is not None and isinstance(
                self.feed_store, PostgresControlPlaneStore
            ):
                raise OperatorMutationPersistenceError(
                    f"Feed delete/audit transaction failed: {exc}"
                ) from exc
            raise ValueError(f"Feed registration delete failed: {exc}") from exc

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
            stream = self.streams.get(stream_id)
            if stream:
                with self.control_plane_lock:
                    stream.finish_run()
                    mark_assignment_dirty(self)
            detail = f": {last_line}" if last_line else ""
            self.fail(f"Feed process exited with code {code}{detail}", stream_id)


def profile_for(protocol: str, mode: str) -> str:
    if protocol == "srt":
        return PROFILE_OPTIONS[mode][1]
    if protocol == "dash":
        return f"profiles/dash-{mode.replace('_', '-')}.yaml"
    raise ValueError(f"Unsupported protocol: {protocol}")


def stream_registration(stream: FeedRecord) -> dict:
    return {
        "id": stream.id,
        "name": stream.name,
        "source": stream.source,
        "external_url": stream.external_url,
        "protocol": stream.protocol,
        "mode": stream.mode,
        "http_port": stream.http_port,
        "feed_port": stream.feed_port,
        "width": stream.width,
        "height": stream.height,
        "framerate": stream.framerate,
        "dash_dir": stream.dash_dir,
        "alert_enabled_ids": stream.alert_enabled_ids,
        "alert_delay_seconds": stream.alert_delay_seconds,
    }


def external_validation_config(stream: FeedRecord):
    config = load_profile(profile_for(stream.protocol, "normal"))
    return replace(
        config,
        protocol=stream.protocol,
        width=stream.width,
        height=stream.height,
        framerate=stream.framerate,
        external_endpoint=stream.endpoint,
        passive=True,
    )


def next_stream_number(streams: dict[str, FeedRecord]) -> int:
    highest = 0
    for stream_id in streams:
        prefix, _, suffix = stream_id.rpartition("-")
        if prefix == "stream" and suffix.isdigit():
            highest = max(highest, int(suffix))
    return highest + 1


def parse_uuid_field(value, field_name: str) -> uuid.UUID:
    if not isinstance(value, str) or not value:
        raise WorkerReportValidationError(f"{field_name} must be a UUID string")
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise WorkerReportValidationError(f"{field_name} must be a UUID string") from exc


class GuiHandler(BaseHTTPRequestHandler):
    state: GuiState

    def _operation_path(self) -> str:
        return (urlparse(self.path).path or "/")[:512]

    def _durable_denial_audit(
        self,
        action: str,
        reason: str,
        *,
        resource_type: str = "http_request",
        resource_id: str = "",
        principal: Principal | None = None,
    ):
        store = durable_control_store(self.state)
        if store is None:
            return
        try:
            store.append_audit_event(
                uuid.uuid4(),
                principal_kind=principal.kind if principal else "unknown",
                principal_subject=principal.subject[:512] if principal else "anonymous",
                action=action,
                resource_type=resource_type,
                resource_id=(resource_id or self._operation_path())[:512],
                outcome="denied",
                occurred_at=datetime.now(timezone.utc),
                payload={
                    "operation": self._operation_path(),
                    "method": self.command,
                    "reason": str(reason)[:2000],
                    "remote": self.client_address[0],
                },
            )
        except Exception as exc:
            # The request remains denied. Emit an explicit audit-gap event rather
            # than hiding a PostgreSQL/outbox failure.
            audit_event(
                "durable_audit",
                "failed",
                principal,
                operation=self._operation_path(),
                reason=str(exc),
                remote=self.client_address[0],
            )

    def _mutation_audit(self, principal: Principal, action: str) -> dict:
        return {
            "event_id": uuid.uuid4(),
            "principal_kind": principal.kind,
            "principal_subject": principal.subject,
            "action": action,
            "outcome": "succeeded",
            "occurred_at": datetime.now(timezone.utc),
            "payload": {
                "operation": self._operation_path(),
                "method": self.command,
                "remote": self.client_address[0],
            },
        }

    def _send_mutation_persistence_error(
        self, exc: Exception, principal: Principal | None = None
    ):
        audit_event(
            "operator_write",
            "failed",
            principal,
            operation=self._operation_path(),
            reason=str(exc),
            remote=self.client_address[0],
        )
        self._send_json(
            {
                "ok": False,
                "error": "persistent mutation and durable audit did not commit",
            },
            status=503,
        )

    def _authorize_worker(self, worker_id: str) -> Principal | None:
        try:
            principal = self.state.security.authenticate_worker(self.headers, worker_id)
        except (AuthenticationError, AuthorizationError) as exc:
            status = 401 if isinstance(exc, AuthenticationError) else 403
            denied_principal = getattr(exc, "principal", None)
            audit_event("worker_auth", "denied", denied_principal, workerId=worker_id, reason=str(exc), remote=self.client_address[0])
            self._durable_denial_audit(
                "worker_auth",
                str(exc),
                resource_type="worker",
                resource_id=worker_id,
                principal=denied_principal,
            )
            self._send_json({"ok": False, "error": str(exc)}, status=status)
            return None
        if self.state.security.enabled:
            audit_event("worker_auth", "allowed", principal, workerId=worker_id, remote=self.client_address[0])
        return principal

    def _authorize_operator(self, *, write: bool) -> Principal | None:
        try:
            principal = self.state.security.authenticate_operator(self.headers, write=write)
        except (AuthenticationError, AuthorizationError) as exc:
            status = 401 if isinstance(exc, AuthenticationError) else 403
            denied_principal = getattr(exc, "principal", None)
            audit_event("operator_auth", "denied", denied_principal, operation=self._operation_path(), reason=str(exc), remote=self.client_address[0])
            self._durable_denial_audit(
                "operator_auth", str(exc), principal=denied_principal
            )
            self._send_json({"ok": False, "error": str(exc)}, status=status)
            return None
        if self.state.security.enabled:
            audit_event(
                "operator_write" if write else "operator_read",
                "allowed",
                principal,
                operation=self._operation_path(),
                remote=self.client_address[0],
            )
        return principal

    def _authorize_proxy(self) -> bool:
        try:
            self.state.security.authenticate_proxy_request(self.headers)
        except AuthenticationError as exc:
            audit_event("proxy_auth", "denied", operation=self._operation_path(), reason=str(exc), remote=self.client_address[0])
            self._durable_denial_audit("proxy_auth", str(exc))
            self._send_json({"ok": False, "error": str(exc)}, status=401)
            return False
        return True

    def do_GET(self):
        parsed_request = urlparse(self.path)
        path = parsed_request.path
        if path in {"/healthz", "/readyz"}:
            self._send_json({"ok": True, "status": "ready" if path == "/readyz" else "healthy"})
            return
        if path == "/api/workers/assignments":
            params = parse_qs(parsed_request.query)
            worker_id = params.get("worker_id", [""])[0]
            if not isinstance(worker_id, str) or not worker_id or len(worker_id) > MAX_IDENTIFIER_LENGTH:
                self._send_json({"ok": False, "error": "worker_id must be a bounded non-empty string"}, status=400)
                return
            principal = self._authorize_worker(worker_id)
            if principal is None:
                return
            try:
                base_url = request_base_url(self, self.state)
                incarnation = None
                if durable_control_store(self.state) is not None:
                    incarnation = parse_uuid_field(
                        params.get("worker_incarnation_id", [""])[0],
                        "worker_incarnation_id",
                    )
                payload = worker_assignments_payload(
                    self.state,
                    worker_id,
                    base_url,
                    incarnation,
                    principal.subject,
                )
            except WorkerReportValidationError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
                return
            except (LeaseConflict, ReportConflict, WorkerReportConflict) as exc:
                self._send_json({"ok": False, "error": str(exc), "retryAssignment": True}, status=409)
                return
            except PostgresStoreError as exc:
                self._send_json({"ok": False, "error": str(exc), "retryAssignment": True}, status=503)
                return
            self._send_json(payload)
            return
        if path.startswith("/dash/"):
            if not self._authorize_proxy():
                return
            self._send_dash(path.removeprefix("/dash/"))
            return
        if self._authorize_operator(write=False) is None:
            return
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
        if path.startswith("/feeds/") and path.endswith("/preview.jpg"):
            stream_id = unquote(path.removeprefix("/feeds/").removesuffix("/preview.jpg").strip("/"))
            body, content_type = preview_image(self.state, stream_id)
            self._send_bytes(body, content_type)
            return
        if path.startswith("/static/"):
            self._send_static(path.removeprefix("/static/"))
            return
        if path.startswith("/feeds/"):
            stream_id = unquote(path.removeprefix("/feeds/").strip("/"))
            if not stream_id:
                self.send_error(404)
                return
            if self.state.security.enabled:
                with self.state.control_plane_lock:
                    if stream_id not in self.state.streams:
                        self.send_error(404)
                        return
                self._send_html(render_page(request_state_view(self.state, stream_id)))
                return
            if not self.state.select_stream(stream_id):
                self.send_error(404)
                return
            self._send_html(render_page(self.state))
            return
        if path != "/":
            self.send_error(404)
            return
        if self.state.security.enabled:
            self._send_html(render_page(request_state_view(self.state, "")))
        else:
            self.state.clear_selection()
            self._send_html(render_page(self.state))

    def do_POST(self):
        if not self._request_body_allowed():
            return
        path = urlparse(self.path).path
        redirect_stream_id = self.state.selected_stream_id
        operator_principal = None
        if path not in {
            "/api/workers/register",
            "/api/workers/drain",
            "/api/workers/leases/ack",
            "/api/workers/report",
        }:
            operator_principal = self._authorize_operator(write=True)
            if operator_principal is None:
                return
        if path == "/start":
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
            try:
                self.state.apply_mode(
                    mode,
                    protocol,
                    stream_id,
                    audit=self._mutation_audit(
                        operator_principal, "feed.expectation.update"
                    ),
                )
            except OperatorMutationPersistenceError as exc:
                self._send_mutation_persistence_error(exc, operator_principal)
                return
        elif path == "/stop":
            params = self._read_form()
            redirect_stream_id = params.get("stream_id", [self.state.selected_stream_id])[0]
            self.state.stop(redirect_stream_id or None)
        elif path == "/validate":
            params = self._read_form()
            redirect_stream_id = params.get("stream_id", [self.state.selected_stream_id])[0]
            self.state.validate(redirect_stream_id or None)
        elif path == "/streams/create":
            params = self._read_form()
            try:
                stream = self.state.create_stream(
                    name=params.get("name", [""])[0],
                    protocol=params.get("protocol", ["srt"])[0],
                    mode=params.get("mode", ["normal"])[0],
                    source=params.get("source", ["generated"])[0],
                    external_url=params.get("external_url", [""])[0],
                    framerate=params.get("framerate", [self.state.framerate])[0],
                    audit=self._mutation_audit(
                        operator_principal, "feed.create"
                    ),
                )
                redirect_stream_id = stream.id
            except OperatorMutationPersistenceError as exc:
                self._send_mutation_persistence_error(exc, operator_principal)
                return
            except (AuthorizationError, ValueError) as exc:
                self.state.log(str(exc))
        elif path == "/streams/select":
            params = self._read_form()
            stream_id = params.get("stream_id", [self.state.selected_stream_id])[0]
            if stream_id:
                self.state.select_stream(stream_id)
                redirect_stream_id = stream_id
        elif path == "/streams/update":
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
                        source=params.get("source", [None])[0],
                        external_url=params.get("external_url", [None])[0],
                        framerate=params.get("framerate", [None])[0],
                        audit=self._mutation_audit(
                            operator_principal, "feed.update"
                        ),
                    )
                except OperatorMutationPersistenceError as exc:
                    self._send_mutation_persistence_error(exc, operator_principal)
                    return
                except (AuthorizationError, ValueError) as exc:
                    self.state.log(str(exc))
        elif path == "/streams/alerts":
            params = self._read_form()
            stream_id = params.get("stream_id", [self.state.selected_stream_id])[0]
            if stream_id:
                redirect_stream_id = stream_id
                try:
                    action = params.get("alert_action", ["save"])[0]
                    enabled_ids = None if action == "enable_all" else [] if action == "disable_all" else params.get("alert_monitor", [])
                    self.state.update_alert_profile(
                        stream_id,
                        enabled_ids,
                        params.get("alert_delay_seconds", [0])[0],
                        audit=self._mutation_audit(
                            operator_principal, "feed.alert_profile.update"
                        ),
                    )
                except OperatorMutationPersistenceError as exc:
                    self._send_mutation_persistence_error(exc, operator_principal)
                    return
                except ValueError as exc:
                    self.state.log(str(exc))
        elif path == "/streams/events/clear":
            params = self._read_form()
            stream_id = params.get("stream_id", [self.state.selected_stream_id])[0]
            if stream_id:
                redirect_stream_id = stream_id
                if durable_control_store(self.state) is not None:
                    self.state.log(
                        "Durable PostgreSQL alarm history is immutable and cannot be cleared",
                        stream_id,
                    )
                elif clear_monitor_events(self.state, stream_id):
                    self.state.log("Cleared event audit", stream_id)
        elif path == "/api/workers/register":
            payload = self._read_json()
            if not isinstance(payload, dict):
                self._send_json({"ok": False, "error": "worker registration must be an object"}, status=400)
                return
            worker_id = payload.get("workerId", "")
            if not isinstance(worker_id, str) or not worker_id or len(worker_id) > MAX_IDENTIFIER_LENGTH:
                self._send_json({"ok": False, "error": "workerId must be a bounded non-empty string"}, status=400)
                return
            principal = self._authorize_worker(worker_id)
            if principal is None:
                return
            if any(
                field in payload and not isinstance(payload[field], dict)
                for field in ("capabilities", "capacity")
            ) or not isinstance(payload.get("softwareVersion", ""), str):
                self._send_json(
                    {
                        "ok": False,
                        "error": "capabilities/capacity must be objects and softwareVersion must be a string",
                    },
                    status=422,
                )
                return
            try:
                incarnation = None
                if durable_control_store(self.state) is not None:
                    incarnation = parse_uuid_field(
                        payload.get("workerIncarnationId"), "workerIncarnationId"
                    )
                response = register_worker(
                    self.state,
                    worker_id,
                    worker_incarnation_id=incarnation,
                    certificate_subject=principal.subject,
                    capabilities=payload.get("capabilities")
                    if "capabilities" in payload
                    and isinstance(payload.get("capabilities"), dict)
                    else None,
                    capacity=payload.get("capacity")
                    if "capacity" in payload and isinstance(payload.get("capacity"), dict)
                    else None,
                    software_version=str(payload.get("softwareVersion", ""))[:128],
                )
            except WorkerReportValidationError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=422)
                return
            except LeaseConflict as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "retryRegistration": True},
                    status=409,
                )
                return
            except PostgresStoreError as exc:
                self._send_json({"ok": False, "error": str(exc), "retryRegistration": True}, status=503)
                return
            self._send_json(response)
            return
        elif path == "/api/workers/drain":
            payload = self._read_json()
            if not isinstance(payload, dict):
                self._send_json({"ok": False, "error": "worker drain must be an object"}, status=400)
                return
            worker_id = payload.get("workerId", "")
            if not isinstance(worker_id, str) or not worker_id or len(worker_id) > MAX_IDENTIFIER_LENGTH:
                self._send_json({"ok": False, "error": "workerId must be a bounded non-empty string"}, status=400)
                return
            if self._authorize_worker(worker_id) is None:
                return
            try:
                incarnation = None
                if durable_control_store(self.state) is not None:
                    if payload.get("apiVersion") != WORKER_API_VERSION_V2:
                        raise WorkerReportValidationError("worker drain requires worker API v2")
                    incarnation = parse_uuid_field(
                        payload.get("workerIncarnationId"), "workerIncarnationId"
                    )
                response = drain_registered_worker(self.state, worker_id, incarnation)
            except WorkerReportValidationError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=422)
                return
            except (LeaseConflict, WorkerReportConflict) as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=409)
                return
            except PostgresStoreError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=503)
                return
            self._send_json(response)
            return
        elif path == "/api/workers/leases/ack":
            payload = self._read_json()
            if not isinstance(payload, dict):
                self._send_json({"ok": False, "error": "lease acknowledgement must be an object"}, status=400)
                return
            worker_id = payload.get("workerId", "")
            if not isinstance(worker_id, str) or not worker_id or len(worker_id) > MAX_IDENTIFIER_LENGTH:
                self._send_json({"ok": False, "error": "workerId must be a bounded non-empty string"}, status=400)
                return
            if self._authorize_worker(worker_id) is None:
                return
            try:
                response = acknowledge_worker_leases(self.state, worker_id, payload)
            except WorkerReportValidationError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=422)
                return
            except (LeaseConflict, ReportConflict) as exc:
                self._send_json({"ok": False, "error": str(exc), "retryAssignment": True}, status=409)
                return
            except PostgresStoreError as exc:
                self._send_json({"ok": False, "error": str(exc), "retryAssignment": True}, status=503)
                return
            self._send_json(response)
            return
        elif path == "/api/workers/report":
            payload = self._read_json()
            if not isinstance(payload, dict):
                self._send_json({"ok": False, "error": "worker report must be an object"}, status=400)
                return
            worker_id = payload.get("workerId", "")
            if not isinstance(worker_id, str) or not worker_id or len(worker_id) > MAX_IDENTIFIER_LENGTH:
                self._send_json({"ok": False, "error": "workerId must be a bounded non-empty string"}, status=400)
                return
            if self._authorize_worker(worker_id) is None:
                return
            try:
                if durable_control_store(self.state) is not None:
                    if payload.get("apiVersion") != WORKER_API_VERSION_V2:
                        raise WorkerReportConflict(
                            "PostgreSQL-backed workers must use worker API v2"
                        )
                    response = apply_durable_worker_report(self.state, worker_id, payload)
                else:
                    response = apply_worker_report(
                        self.state,
                        worker_id,
                        payload.get("streamIds", []),
                        payload.get("state", {}),
                        payload,
                    )
            except (ReportConflict, WorkerReportConflict) as exc:
                self._send_json({"ok": False, "error": str(exc), "retryAssignment": True}, status=409)
                return
            except WorkerReportValidationError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=422)
                return
            except (PostgresStoreError, WorkerReportPersistenceError) as exc:
                self._send_json({"ok": False, "error": str(exc), "retryReport": True}, status=503)
                return
            status = 409 if response.get("retryAssignment") else 200
            self._send_json(response, status=status)
            return
        elif path == "/streams/delete":
            params = self._read_form()
            stream_id = params.get("stream_id", [self.state.selected_stream_id])[0]
            if stream_id:
                try:
                    self.state.delete_stream(
                        stream_id,
                        audit=self._mutation_audit(
                            operator_principal, "feed.delete"
                        ),
                    )
                except OperatorMutationPersistenceError as exc:
                    self._send_mutation_persistence_error(exc, operator_principal)
                    return
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

    def _send_json(self, payload, status: int = 200):
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
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

    def _request_body_allowed(self) -> bool:
        if self.headers.get("Transfer-Encoding"):
            self._send_json({"ok": False, "error": "chunked request bodies are not supported"}, status=400)
            return False
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json({"ok": False, "error": "invalid Content-Length"}, status=400)
            return False
        if length < 0:
            self._send_json({"ok": False, "error": "invalid Content-Length"}, status=400)
            return False
        if length > MAX_REQUEST_BODY_BYTES:
            self._send_json({"ok": False, "error": "request body exceeds 1 MiB limit"}, status=413)
            return False
        return True

    def _read_form(self):
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = self.rfile.read(length).decode("utf-8") if length else ""
        except UnicodeDecodeError:
            return {}
        return parse_qs(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            return json.loads(body or "{}")
        except (json.JSONDecodeError, RecursionError, UnicodeDecodeError):
            return {}


def request_state_view(state: GuiState, selected_stream_id: str) -> GuiState:
    """Create a request-local shallow view without mutating shared selection."""

    view = copy.copy(state)
    view.selected_stream_id = selected_stream_id if selected_stream_id in state.streams else ""
    view._sync_from_active()
    return view


def render_page(state: GuiState) -> str:
    active = state.active_stream
    monitor = monitor_payload(state)
    active_alarms = [alarm for alarm in monitor.get("alarms", []) if alarm.get("active")]
    alert_options = monitor.get("monitors", [])
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
    selected_source = active.source if active else "generated"
    source_options = "\n".join(
        f'<option value="{html.escape(source)}"{" selected" if source == selected_source else ""}>{html.escape(label)}</option>'
        for source, label in SOURCE_OPTIONS.items()
    )
    framerate_options = "\n".join(
        f'<option value="{html.escape(option["value"])}"{" selected" if option["value"] == state.framerate else ""}>{html.escape(option["label"])}</option>'
        for option in supported_frame_rate_options()
    )
    controls = controls_for_mode(active.mode if active else state.mode)
    checked = {field: " checked" if controls[field] else "" for field in CONTROL_FIELDS}
    stream_rows = "\n".join(
        f"""<tr>
          <td><a href="{html.escape(feed_path(stream.id))}"><img alt="{html.escape(stream.name)} preview" src="{html.escape(feed_path(stream.id))}/preview.jpg" style="width:8.5rem;aspect-ratio:16/9;object-fit:cover;border-radius:0.45rem;background:#050505;"></a></td>
          <td><strong>{html.escape(stream.name)}</strong><br>{html.escape(SOURCE_OPTIONS[stream.source])} · {html.escape(stream.protocol.upper())}{'' if stream.source == "external" else ' · ' + html.escape(stream.mode)}</td>
          <td>{html.escape(stream.status)}</td>
          <td>{html.escape(stream.endpoint)}</td>
          <td>{'' if stream.source == "external" else 'Frame rate: ' + html.escape(stream.framerate) + ' fps<br>'}Bit rate (est.): {html.escape(stream.metrics()["bitrateLabel"])}<br>Outbound (est.): {html.escape(stream.metrics()["outboundLabel"])}<br>Uptime: {html.escape(str(stream.metrics()["uptimeSeconds"]))}s</td>
          <td><a class="button secondary" href="{html.escape(feed_path(stream.id))}">Open</a></td>
        </tr>"""
        for stream in state.streams.values()
    )
    alarm_rows = "\n".join(
        f"""<tr>
          <td>{html.escape(alarm.get("streamName", ""))}</td>
          <td>{html.escape(alarm.get("monitorName", ""))}</td>
          <td>{html.escape(alarm.get("severity", ""))}</td>
          <td>{html.escape(alarm.get("status", ""))}</td>
          <td>{html.escape(alarm.get("message", ""))}</td>
        </tr>"""
        for alarm in monitor.get("alarms", [])[:20]
    )
    create_form = f"""
  <h3>Create generated feed</h3>
  <form method="post" action="/streams/create" class="row">
    <input type="hidden" name="source" value="generated">
    <label>Name <input name="name" value="Feed {len(state.streams) + 1}"></label>
    <label>Protocol <select name="protocol">{protocol_options}</select></label>
    <label>Mode <select name="mode">{options}</select></label>
    <label>Frame rate <select name="framerate">{framerate_options}</select></label>
    <button type="submit">Create stream</button>
  </form>
  <h3>Register external feed</h3>
  <form method="post" action="/streams/create" class="row">
    <input type="hidden" name="source" value="external">
    <label>Name <input name="name" value="External feed"></label>
    <label>Protocol <select name="protocol">{protocol_options}</select></label>
    <label>External URL <input name="external_url" placeholder="srt://host:port or https://host/manifest.mpd"></label>
    <button type="submit">Register feed</button>
  </form>
  <div class="empty-state">Open an existing feed or create a new one.</div>"""
    selected_detail = ""
    if active:
        metrics = active.metrics()
        active_framerate_options = "\n".join(
            f'<option value="{html.escape(option["value"])}"{" selected" if option["value"] == active.framerate else ""}>{html.escape(option["label"])}</option>'
            for option in supported_frame_rate_options()
        )
        alert_enabled = active.alert_enabled_ids
        alert_inputs = "\n".join(
            f"""<label class="control"><input type="checkbox" name="alert_monitor" value="{html.escape(option.get("id", ""))}"{" checked" if alert_enabled is None or option.get("id") in alert_enabled else ""}> {html.escape(option.get("name", option.get("id", "")))}</label>"""
            for option in alert_options
        )
        alert_profile_form = (
            f"""
  <form method="post" action="/streams/alerts">
    <fieldset>
      <legend>Alert profile</legend>
      <input type="hidden" name="stream_id" value="{html.escape(state.selected_stream_id)}">
      <div class="row">
        <label>Alarm delay seconds <input type="number" min="0" step="1" name="alert_delay_seconds" value="{active.alert_delay_seconds}"></label>
        <button type="submit" name="alert_action" value="save">Save selected</button>
        <button type="submit" name="alert_action" value="enable_all">Enable all</button>
        <button type="submit" name="alert_action" value="disable_all">Disable all</button>
      </div>
      <div class="row">{alert_inputs}</div>
    </fieldset>
  </form>"""
            if alert_options
            else """
  <fieldset>
    <legend>Alert profile</legend>
    <div class="empty-state">Monitor not running</div>
  </fieldset>"""
        )
        config_fields = (
            f"""
    <label>Mode <select name="mode">{options}</select></label>
    <label>Frame rate <select name="framerate">{active_framerate_options}</select></label>"""
            if active.source != "external"
            else f"""
    <label>External URL <input name="external_url" value="{html.escape(active.external_url)}" placeholder="srt://host:port or https://host/manifest.mpd"></label>"""
        )
        selected_detail = f"""
  <form method="post" action="/streams/update" class="row">
    <input type="hidden" name="stream_id" value="{html.escape(state.selected_stream_id)}">
    <label>Selected name <input name="name" value="{html.escape(active.name)}"></label>
    <label>Source <select name="source">{source_options}</select></label>
    <label>Protocol <select name="protocol">{protocol_options}</select></label>
    {config_fields}
    <button type="submit">Update stream</button>
  </form>
  <form method="post" action="/streams/delete" class="row">
    <input type="hidden" name="stream_id" value="{html.escape(state.selected_stream_id)}">
    <button class="secondary" type="submit">Delete selected stream</button>
  </form>
  <div>Status: <strong>{status}</strong></div>
  <div>Intentional outage: <strong>{outage}</strong></div>
  <div>Last error: <strong>{last_error}</strong></div>
  <div>Bit rate (est.): <strong>{html.escape(metrics["bitrateLabel"])}</strong></div>
  {'' if active.source == "external" else f'<div>Frame rate: <strong>{html.escape(active.framerate)} fps</strong></div>'}
  <div>Outbound total (est.): <strong>{html.escape(metrics["outboundLabel"])}</strong></div>
  <div>Uptime: <strong>{html.escape(str(metrics["uptimeSeconds"]))}s</strong></div>
  <div>Video frames: <strong>{html.escape(metrics["videoFramesLabel"])}</strong></div>
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
  {alert_profile_form}
  {'' if durable_control_store(state) is not None else f'''<form method="post" action="/streams/events/clear" class="row">
    <input type="hidden" name="stream_id" value="{html.escape(state.selected_stream_id)}">
    <button class="secondary" type="submit">Clear event audit</button>
  </form>'''}
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
    body {{ font-family: var(--font-sans); font-size: var(--text-base); margin: 0; background: var(--surface-app); color: var(--text-1); }}
    main {{ display: grid; gap: var(--space-6); max-width: 72rem; margin: 0 auto; padding: var(--space-7); }}
    button, .button {{ border: 1px solid transparent; border-radius: var(--radius-md); background: var(--accent); color: var(--on-accent); min-height: var(--control-md); padding: 0 14px; text-decoration: none; font-weight: var(--weight-medium); }}
    button.secondary, .button.secondary {{ background: var(--surface-raised); border-color: var(--border-strong); color: var(--text-1); }}
    #endpoint {{ width: min(100%, 34rem); padding: 0 10px; border: 1px solid var(--border-strong); border-radius: var(--radius-md); background: var(--surface-inset); color: var(--text-1); }}
    fieldset {{ border: 1px solid var(--border); border-radius: var(--radius-lg); padding: var(--space-5); }}
    label.control {{ display: inline-flex; gap: 0.35rem; align-items: center; margin-right: 0.75rem; }}
    pre {{ background: var(--surface-inset); color: var(--text-2); min-height: 12rem; padding: var(--space-6); overflow: auto; border: 1px solid var(--border-subtle); border-radius: var(--radius-md); }}
    .row {{ display: flex; gap: var(--space-4); flex-wrap: wrap; align-items: center; }}
    .shell {{ background: var(--surface-card); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: var(--space-6); }}
  </style>
</head>
<body>
<div id="app"></div>
<script id="initial-state" type="application/json">{script_json(state_payload(state))}</script>
<main>
  <div id="app-fallback" class="shell">
  <h1>Video Feed Simulator</h1>
  <h2>Active feeds</h2>
  <table class="feed-table">
    <thead><tr><th>Preview</th><th>Feed</th><th>Status</th><th>Endpoint</th><th>Metrics</th><th>Actions</th></tr></thead>
    <tbody>{stream_rows}</tbody>
  </table>
  <h2>Monitor alarms ({len(active_alarms)} active)</h2>
  <table class="feed-table">
    <thead><tr><th>Feed</th><th>Monitor</th><th>Severity</th><th>Status</th><th>Message</th></tr></thead>
    <tbody>{alarm_rows}</tbody>
  </table>
  {selected_detail if active else create_form}
  </div>
</main>
<script type="module" src="/static/app.js"></script>
</body>
</html>"""


def state_payload(state: GuiState) -> dict:
    active = state.active_stream
    controls = controls_for_mode(active.mode if active else state.mode)
    monitor = monitor_payload(state)
    return {
        "selectedStreamId": state.selected_stream_id,
        "feedListUrl": "/",
        "streams": [stream_payload(stream) for stream in state.streams.values()],
        "status": state.status,
        "protocol": state.protocol,
        "protocols": [{"value": value, "label": label} for value, label in PROTOCOL_OPTIONS.items()],
        "sources": [{"value": value, "label": label} for value, label in SOURCE_OPTIONS.items()],
        "framerate": state.framerate,
        "framerates": supported_frame_rate_options(),
        "mode": state.mode,
        "intentionalOutage": state.intentional_outage,
        "endpoint": state.endpoint,
        "lastError": state.last_error or (active.last_error if active else "") or "none",
        "validationOutput": state.validation_output or (active.validation_output if active else "") or "not run",
        "metrics": active.metrics() if active else None,
        "previewAvailable": bool(active) and active.source == "generated" and state.status == "running" and controls["video"],
        "previewUrl": "/preview.jpg",
        "logs": (state.logs or (active.logs if active else []))[-80:],
        "modes": [
            {"value": mode, "label": label, "controls": MODE_CONTROLS[mode]}
            for mode, (label, _) in PROFILE_OPTIONS.items()
        ],
        "controls": controls,
        "monitor": monitor,
        "alertOptions": monitor.get("monitors", []),
    }


def stream_payload(stream: FeedRecord) -> dict:
    return {
        "id": stream.id,
        "name": stream.name,
        "url": feed_path(stream.id),
        "protocol": stream.protocol,
        "source": stream.source,
        "sourceLabel": SOURCE_OPTIONS[stream.source],
        "externalUrl": stream.external_url,
        "mode": stream.mode,
        "framerate": stream.framerate,
        "framerateLabel": f"{stream.framerate} fps",
        "alertProfile": alert_profile_payload(stream.alert_enabled_ids, stream.alert_delay_seconds),
        "status": stream.status,
        "endpoint": stream.endpoint,
        "intentionalOutage": stream.intentional_outage,
        "lastError": stream.last_error or "none",
        "validationOutput": stream.validation_output or "not run",
        "metrics": stream.metrics(),
        "previewAvailable": stream.source == "generated" and stream.status == "running" and controls_for_mode(stream.mode)["video"],
        "previewUrl": f"{feed_path(stream.id)}/preview.jpg",
        "logs": stream.logs[-80:],
    }


def mark_assignment_dirty(state: GuiState):
    """Advance the process-local assignment fence after a relevant mutation."""

    with state.control_plane_lock:
        state.assignment_generation += 1
        state.assignment_dirty = True


def monitoring_stream_contract(stream: FeedRecord) -> dict:
    return {
        "id": stream.id,
        "status": stream.status,
        "source": stream.source,
        "protocol": stream.protocol,
        "mode": stream.mode,
        "externalUrl": stream.external_url,
        "feedPort": stream.feed_port,
        "width": stream.width,
        "height": stream.height,
        "framerate": str(stream.framerate),
        "alertEnabledIds": stream.alert_enabled_ids,
        "alertDelaySeconds": stream.alert_delay_seconds,
    }


def _expire_workers_locked(state: GuiState, now: float) -> list[str]:
    active = {worker_id: seen for worker_id, seen in state.worker_seen.items() if now - seen <= WORKER_TTL_SECONDS}
    if set(active) != set(state.worker_seen):
        state.worker_seen = active
        state.assignment_generation += 1
        state.assignment_dirty = True
    return sorted(active)


def _refresh_assignment_snapshot_locked(state: GuiState, now: float | None = None) -> tuple[list[str], list[FeedRecord], list[dict]]:
    now = time.time() if now is None else now
    worker_ids = _expire_workers_locked(state, now)
    running_streams = sorted(
        (stream for stream in state.streams.values() if stream.status == "running"),
        key=lambda item: item.id,
    )
    stream_contracts = [monitoring_stream_contract(stream) for stream in running_streams]
    fingerprint = assignment_fingerprint(worker_ids, stream_contracts)
    if state.assignment_dirty:
        state.assignment_fingerprint = fingerprint
        state.assignment_dirty = False
    elif fingerprint != state.assignment_fingerprint:
        state.assignment_generation += 1
        state.assignment_fingerprint = fingerprint
    return worker_ids, running_streams, stream_contracts


def current_assignment_contract(state: GuiState, worker_id: str, now: float | None = None) -> tuple[AssignmentContract, list[FeedRecord]]:
    worker_id = worker_id.strip()
    if not worker_id:
        raise WorkerReportValidationError("worker_id is required")
    with state.control_plane_lock:
        worker_ids, running_streams, stream_contracts = _refresh_assignment_snapshot_locked(state, now)
        if worker_id not in worker_ids:
            raise WorkerReportConflict("worker is not registered or its assignment has expired")
        worker_index = worker_ids.index(worker_id)
        assigned_streams = [
            stream for index, stream in enumerate(running_streams) if index % len(worker_ids) == worker_index
        ]
        contracts_by_id = {item["id"]: item for item in stream_contracts}
        assigned_contracts = [contracts_by_id[stream.id] for stream in assigned_streams]
        contract = AssignmentContract(
            api_version=WORKER_API_VERSION,
            control_plane_instance_id=state.control_plane_instance_id,
            assignment_generation=state.assignment_generation,
            assignment_token=assignment_token(
                state.control_plane_instance_id,
                state.assignment_generation,
                worker_id,
                assigned_contracts,
            ),
            worker_id=worker_id,
            stream_ids=tuple(stream.id for stream in assigned_streams),
        )
        return contract, assigned_streams


def durable_control_store(state: GuiState) -> PostgresControlPlaneStore | None:
    return state.feed_store if isinstance(state.feed_store, PostgresControlPlaneStore) else None


def worker_assignments_payload(
    state: GuiState,
    worker_id: str,
    base_url: str,
    worker_incarnation_id: uuid.UUID | None = None,
    certificate_subject: str = "",
) -> dict:
    store = durable_control_store(state)
    if store is not None:
        if worker_incarnation_id is None:
            raise WorkerReportValidationError("worker_incarnation_id is required for worker API v2")
        worker = register_worker(
            state,
            worker_id,
            worker_incarnation_id=worker_incarnation_id,
            certificate_subject=certificate_subject or worker_id,
        )
        with state.control_plane_lock:
            worker_records = store.active_worker_records()
            worker_ids = [item["id"] for item in worker_records]
            if worker_id not in worker_ids:
                raise WorkerReportConflict("worker is not fresh and active")
            running_streams = sorted(
                (stream for stream in state.streams.values() if stream.status == "running"),
                key=lambda item: item.id,
            )
            assignments, capacity_shortfall = capacity_aware_assignments(
                worker_records, running_streams
            )
            assigned_streams = assignments[worker_id]
            store.revoke_unassigned_leases(
                worker_id,
                worker_incarnation_id,
                (stream.id for stream in assigned_streams),
            )
            streams = []
            for stream in assigned_streams:
                lease = store.reconcile_lease(
                    stream.id,
                    worker_id,
                    worker_incarnation_id,
                    ttl_seconds=max(1, int(WORKER_TTL_SECONDS)),
                )
                stream_contract = control_plane_stream_payload(stream, base_url, worker_id)
                stream_contract["lease"] = {
                    "epoch": lease.epoch,
                    "configVersion": lease.config_version,
                    "expiresAt": lease.expires_at.astimezone(timezone.utc).isoformat(),
                    "state": lease.state,
                }
                streams.append(stream_contract)
            return {
                "apiVersion": WORKER_API_VERSION_V2,
                "workerId": worker["id"],
                "workerIncarnationId": str(worker_incarnation_id),
                "workers": worker_records,
                "streams": streams,
                "capacityShortfall": capacity_shortfall,
            }

    worker = register_worker(state, worker_id)
    with state.control_plane_lock:
        contract, assigned_streams = current_assignment_contract(state, worker["id"])
        streams = [control_plane_stream_payload(stream, base_url, worker["id"]) for stream in assigned_streams]
        return {
            **contract.payload(),
            "workerId": worker["id"],
            "workers": worker_status_payload(state),
            "streams": streams,
        }


def control_plane_stream_payload(stream: FeedRecord, base_url: str, worker_id: str) -> dict:
    payload = stream_payload(stream)
    payload["assignedWorkerId"] = worker_id
    if stream.protocol == "dash" and stream.source == "generated":
        endpoint = f"{base_url.rstrip('/')}/dash/{quote(stream.id, safe='')}/manifest.mpd"
        payload["endpoint"] = endpoint
        payload["monitorEndpoint"] = endpoint
    return payload


def capacity_aware_assignments(
    worker_records: list[dict], streams: list[FeedRecord]
) -> tuple[dict[str, list[FeedRecord]], int]:
    """Assign streams without exceeding advertised durable-worker capacity."""
    workers = sorted(worker_records, key=lambda item: item["id"])
    assignments = {worker["id"]: [] for worker in workers}
    if not workers:
        return assignments, len(streams)
    capacities = {
        worker["id"]: normalize_worker_capacity(worker.get("capacity")) or {}
        for worker in workers
    }
    admission_fields = ("maxStreams", *PROTOCOL_CAPACITY_FIELDS.values())
    if not any(
        field in capacity
        for capacity in capacities.values()
        for field in admission_fields
    ):
        for index, stream in enumerate(streams):
            assignments[workers[index % len(workers)]["id"]].append(stream)
        return assignments, 0

    protocol_counts = {
        worker["id"]: {protocol: 0 for protocol in PROTOCOL_CAPACITY_FIELDS}
        for worker in workers
    }
    unassigned = 0
    for stream in streams:
        protocol_field = PROTOCOL_CAPACITY_FIELDS.get(stream.protocol)
        eligible = []
        for worker in workers:
            worker_id = worker["id"]
            capacity = capacities[worker_id]
            if len(assignments[worker_id]) >= capacity.get("maxStreams", math.inf):
                continue
            if (
                protocol_field
                and protocol_counts[worker_id][stream.protocol]
                >= capacity.get(protocol_field, math.inf)
            ):
                continue
            eligible.append(worker)
        if not eligible:
            unassigned += 1
            continue

        def load(worker: dict) -> tuple[float, int, str]:
            worker_id = worker["id"]
            capacity = capacities[worker_id]
            utilization = []
            if "maxStreams" in capacity:
                utilization.append(
                    len(assignments[worker_id]) / capacity["maxStreams"]
                )
            if protocol_field and protocol_field in capacity:
                utilization.append(
                    protocol_counts[worker_id][stream.protocol]
                    / capacity[protocol_field]
                )
            return (
                max(utilization, default=1.0),
                len(assignments[worker_id]),
                worker_id,
            )

        selected = min(
            eligible,
            key=load,
        )
        assignments[selected["id"]].append(stream)
        if stream.protocol in PROTOCOL_CAPACITY_FIELDS:
            protocol_counts[selected["id"]][stream.protocol] += 1
    return assignments, unassigned


def normalize_worker_capacity(capacity: dict | None) -> dict | None:
    if capacity is None:
        return None
    for field in (
        "maxStreams",
        "maxSrtStreams",
        "maxDashStreams",
        "maxConcurrentChecks",
        "maxConcurrentDeepChecks",
    ):
        value = capacity.get(field)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= MAX_WORKER_STREAMS
        ):
            raise WorkerReportValidationError(
                f"capacity.{field} must be an integer between 1 and {MAX_WORKER_STREAMS}"
            )
    for field in (
        "streamBudgetSeconds",
        "deepCheckIntervalSeconds",
        "batchBudgetSeconds",
    ):
        value = capacity.get(field)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise WorkerReportValidationError(
                f"capacity.{field} must be a finite number greater than zero"
            )
    return dict(capacity)


def register_worker(
    state: GuiState,
    worker_id: str,
    *,
    worker_incarnation_id: uuid.UUID | None = None,
    certificate_subject: str = "",
    capabilities: dict | None = None,
    capacity: dict | None = None,
    software_version: str = "",
) -> dict:
    worker_id = worker_id.strip()
    if not worker_id:
        raise ValueError("worker_id is required")
    capacity = normalize_worker_capacity(capacity)
    store = durable_control_store(state)
    if store is not None:
        if worker_incarnation_id is None:
            raise WorkerReportValidationError("workerIncarnationId is required for worker API v2")
        store.register_worker(
            worker_id,
            worker_incarnation_id,
            certificate_subject or worker_id,
            capabilities=capabilities,
            capacity=capacity,
            software_version=software_version,
        )
    with state.control_plane_lock:
        now = time.time()
        active = _expire_workers_locked(state, now)
        if worker_id not in active:
            state.assignment_generation += 1
            state.assignment_dirty = True
        state.worker_seen[worker_id] = now
        return {
            "id": worker_id,
            "lastSeenAt": round(now, 3),
            "apiVersion": WORKER_API_VERSION_V2 if store is not None else WORKER_API_VERSION,
            "workerIncarnationId": str(worker_incarnation_id) if worker_incarnation_id else "",
            "controlPlaneInstanceId": state.control_plane_instance_id,
        }


def active_worker_ids(state: GuiState, now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    with state.control_plane_lock:
        return _expire_workers_locked(state, now)


def drain_registered_worker(
    state: GuiState,
    worker_id: str,
    worker_incarnation_id: uuid.UUID | None = None,
) -> dict:
    store = durable_control_store(state)
    if store is not None:
        if worker_incarnation_id is None:
            raise WorkerReportValidationError("workerIncarnationId is required")
        stream_ids = store.drain_worker(worker_id, worker_incarnation_id)
    else:
        with state.control_plane_lock:
            if worker_id not in state.worker_seen:
                raise WorkerReportConflict("worker is not registered")
            state.worker_seen.pop(worker_id)
            state.assignment_generation += 1
            state.assignment_dirty = True
        stream_ids = []
    return {
        "ok": True,
        "workerId": worker_id,
        "workerIncarnationId": str(worker_incarnation_id) if worker_incarnation_id else "",
        "drainingStreamIds": stream_ids,
    }


def worker_status_payload(state: GuiState) -> list[dict]:
    with state.control_plane_lock:
        return [{"id": worker_id, "lastSeenAt": round(seen, 3)} for worker_id, seen in sorted(state.worker_seen.items())]


def request_base_url(handler: BaseHTTPRequestHandler, state: GuiState | None = None) -> str:
    if state and state.worker_base_url:
        return state.worker_base_url
    host = handler.headers.get("Host") or f"{handler.server.server_address[0]}:{handler.server.server_address[1]}"
    if any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-:[]" for character in host):
        raise WorkerReportValidationError("invalid Host header")
    scheme = "http"
    if state and state.security.enabled:
        forwarded_proto = (handler.headers.get("X-Forwarded-Proto") or "").lower()
        if forwarded_proto not in {"http", "https"}:
            raise WorkerReportValidationError("trusted proxy must provide X-Forwarded-Proto")
        scheme = forwarded_proto
    return f"{scheme}://{host}"


def script_json(payload) -> str:
    return json.dumps(payload).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def preview_image(state: GuiState, stream_id: str | None = None) -> tuple[bytes, str]:
    stream = state.streams.get(stream_id) if stream_id else state.active_stream
    if not stream:
        return preview_placeholder("Create a feed"), "image/svg+xml"
    if stream.source == "external":
        return preview_placeholder("External preview unavailable"), "image/svg+xml"
    controls = controls_for_mode(stream.mode)
    running = stream.status == "running" or (stream.id == state.selected_stream_id and state.status == "running")
    if not running:
        return preview_placeholder("Feed stopped"), "image/svg+xml"
    if not controls["video"]:
        return preview_placeholder("No video track"), "image/svg+xml"

    preview_width = min(640, stream.width)
    preview_height = max(1, round(stream.height * preview_width / stream.width))
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
                f"video/x-raw,width={stream.width},height={stream.height},framerate={frame_rate_fraction(stream.framerate)}",
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
    monitor = monitor_payload(state)
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
            "monitor:",
            f"active_alarms={sum(1 for alarm in monitor.get('alarms', []) if alarm.get('active'))}",
            f"events={len(monitor.get('events', []))}",
            "",
            "streams:",
            *[
                (
                    f"{stream.id} name={stream.name} protocol={stream.protocol} mode={stream.mode} "
                    f"source={stream.source} "
                    f"status={stream.status} bitrate={stream.metrics()['bitrateLabel']} "
                    f"outbound={stream.metrics()['outboundLabel']} uptime={stream.metrics()['uptimeSeconds']}s "
                    f"alert_delay={stream.alert_delay_seconds}s "
                    f"endpoint={stream.endpoint}"
                )
                for stream in state.streams.values()
            ],
            "",
        ]
    )


def monitor_payload(state: GuiState) -> dict:
    store = durable_control_store(state)
    if store is not None:
        try:
            payload = store.monitor_projection_payload()
        except Exception as exc:
            state.log(f"Durable monitor projection read failed: {exc}")
            return {
                "updatedAt": "",
                "alarms": [],
                "events": [],
                "pending": [],
                "monitors": monitor_catalog_payload(),
                "workers": [],
                "probeMetrics": {},
                "workerProbeMetrics": {},
                "eventHistoryMutable": False,
                "connected": False,
            }
        payload["monitors"] = monitor_catalog_payload()
        return payload
    path = monitor_state_path(state)
    if not path.is_file():
        return {
            "updatedAt": "",
            "alarms": [],
            "events": [],
            "pending": [],
            "monitors": monitor_catalog_payload(),
            "workers": [],
            "probeMetrics": {},
            "workerProbeMetrics": {},
            "eventHistoryMutable": True,
            "connected": False,
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "updatedAt": "",
            "alarms": [],
            "events": [],
            "pending": [],
            "monitors": monitor_catalog_payload(),
            "workers": [],
            "probeMetrics": {},
            "workerProbeMetrics": {},
            "eventHistoryMutable": True,
            "connected": False,
        }
    return {
        "updatedAt": payload.get("updatedAt", ""),
        "alarms": payload.get("alarms", [])[-100:],
        "events": payload.get("events", [])[-200:],
        "pending": payload.get("pending", [])[-100:],
        "monitors": payload.get("monitors", []) or monitor_catalog_payload(),
        "workers": payload.get("workers", []),
        "probeMetrics": payload.get("probeMetrics", {}),
        "workerProbeMetrics": payload.get("workerProbeMetrics", {}),
        "eventHistoryMutable": True,
        "connected": True,
    }


def clear_monitor_events(state: GuiState, stream_id: str) -> bool:
    # PostgreSQL event history is immutable operational evidence. The legacy
    # trusted-lab JSON clear action must not silently erase its direct read
    # projection.
    if durable_control_store(state) is not None:
        return False
    path = monitor_state_path(state)
    if not path.is_file():
        return False
    with state.monitor_state_lock:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        events = payload.get("events", [])
        payload["events"] = [event for event in events if event.get("streamId") != stream_id]
        return write_monitor_payload(path, payload)


def acknowledge_worker_leases(state: GuiState, worker_id: str, payload: dict) -> dict:
    store = durable_control_store(state)
    if store is None:
        raise WorkerReportValidationError("lease acknowledgement requires durable control store")
    if payload.get("apiVersion") != WORKER_API_VERSION_V2:
        raise WorkerReportValidationError("lease acknowledgement requires worker API v2")
    incarnation = parse_uuid_field(
        payload.get("workerIncarnationId"), "workerIncarnationId"
    )
    leases = payload.get("leases")
    if not isinstance(leases, list) or len(leases) > MAX_REPORT_STREAMS:
        raise WorkerReportValidationError(
            f"leases must be an array of at most {MAX_REPORT_STREAMS} items"
        )
    acknowledged = []
    seen_streams = set()
    for index, item in enumerate(leases):
        if not isinstance(item, dict):
            raise WorkerReportValidationError(f"leases[{index}] must be an object")
        stream_id = item.get("streamId")
        epoch = item.get("epoch")
        config_version = item.get("configVersion")
        if (
            not isinstance(stream_id, str)
            or not stream_id
            or len(stream_id) > MAX_IDENTIFIER_LENGTH
            or stream_id in seen_streams
        ):
            raise WorkerReportValidationError(
                f"leases[{index}].streamId must be unique and bounded"
            )
        if type(epoch) is not int or epoch < 1:
            raise WorkerReportValidationError(f"leases[{index}].epoch must be a positive integer")
        if type(config_version) is not int or config_version < 1:
            raise WorkerReportValidationError(
                f"leases[{index}].configVersion must be a positive integer"
            )
        seen_streams.add(stream_id)
        lease = store.acknowledge_lease(
            stream_id,
            worker_id,
            incarnation,
            epoch=epoch,
            config_version=config_version,
            ttl_seconds=max(1, int(WORKER_TTL_SECONDS)),
        )
        acknowledged.append(
            {
                "streamId": stream_id,
                "epoch": lease.epoch,
                "configVersion": lease.config_version,
                "expiresAt": lease.expires_at.astimezone(timezone.utc).isoformat(),
                "state": lease.state,
            }
        )
    return {
        "ok": True,
        "apiVersion": WORKER_API_VERSION_V2,
        "workerId": worker_id,
        "workerIncarnationId": str(incarnation),
        "leases": acknowledged,
    }


def parse_observed_at(value) -> datetime:
    if not isinstance(value, str) or not value:
        raise WorkerReportValidationError("state.probeMetrics.observedAt is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkerReportValidationError(
            "state.probeMetrics.observedAt must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WorkerReportValidationError(
            "state.probeMetrics.observedAt must include a timezone"
        )
    return parsed.astimezone(timezone.utc)


def apply_durable_worker_report(
    state: GuiState,
    worker_id: str,
    payload: dict,
) -> dict:
    store = durable_control_store(state)
    if store is None:
        raise WorkerReportConflict("worker API v2 requires durable control store")
    incarnation = parse_uuid_field(
        payload.get("workerIncarnationId"), "workerIncarnationId"
    )
    report_id = parse_uuid_field(payload.get("reportId"), "reportId")
    sequence = payload.get("sequence")
    if type(sequence) is not int or sequence < 1:
        raise WorkerReportValidationError("sequence must be a positive JSON integer")
    stream_ids = payload.get("streamIds")
    if (
        not isinstance(stream_ids, list)
        or len(stream_ids) > MAX_REPORT_STREAMS
        or any(
            not isinstance(stream_id, str)
            or not stream_id
            or len(stream_id) > MAX_IDENTIFIER_LENGTH
            for stream_id in stream_ids
        )
        or len(stream_ids) != len(set(stream_ids))
    ):
        raise WorkerReportValidationError(
            f"streamIds must contain at most {MAX_REPORT_STREAMS} unique bounded strings"
        )
    claimed_stream_ids = set(stream_ids)
    leases = payload.get("leases")
    if not isinstance(leases, list) or len(leases) != len(stream_ids):
        raise WorkerReportValidationError("leases must contain exactly one item per streamId")
    lease_by_stream = {}
    for index, item in enumerate(leases):
        if not isinstance(item, dict):
            raise WorkerReportValidationError(f"leases[{index}] must be an object")
        stream_id = item.get("streamId")
        epoch = item.get("epoch")
        config_version = item.get("configVersion")
        lease_sequence = item.get("sequence")
        if stream_id not in claimed_stream_ids or stream_id in lease_by_stream:
            raise WorkerReportValidationError(
                f"leases[{index}].streamId must match one unique claimed stream"
            )
        if type(epoch) is not int or epoch < 1:
            raise WorkerReportValidationError(f"leases[{index}].epoch must be a positive integer")
        if type(config_version) is not int or config_version < 1:
            raise WorkerReportValidationError(
                f"leases[{index}].configVersion must be a positive integer"
            )
        if type(lease_sequence) is not int or lease_sequence < 1:
            raise WorkerReportValidationError(
                f"leases[{index}].sequence must be a positive integer"
            )
        lease_by_stream[stream_id] = (
            epoch,
            config_version,
            lease_sequence,
        )

    worker_state = payload.get("state")
    scoped_claimed, dropped_claimed = validate_monitor_items(
        worker_state, claimed_stream_ids
    )
    metrics = scoped_claimed.get("probeMetrics")
    if not isinstance(metrics, dict):
        raise WorkerReportValidationError("worker API v2 requires state.probeMetrics")
    observed_at = parse_observed_at(metrics.get("observedAt"))
    metric_items = metrics.get("streams", [])
    metrics_by_stream: dict[str, list[dict]] = {
        stream_id: [] for stream_id in stream_ids
    }
    for item in metric_items:
        metrics_by_stream[item["streamId"]].append(item)
    observations_by_stream: dict[str, list[dict]] = {
        stream_id: [] for stream_id in stream_ids
    }
    for item in scoped_claimed.get("monitorObservations", []):
        observations_by_stream[item["streamId"]].append(item)

    status_by_outcome = {
        "success": "healthy",
        "issue": "unhealthy",
        "error": "error",
        "timeout": "timeout",
        "skipped": "skipped",
    }
    results = []
    result_streams = {}
    for stream_id in sorted(stream_ids):
        stream_metrics = metrics_by_stream[stream_id]
        if not stream_metrics:
            stream_metrics = [
                {
                    "check": "batch",
                    "outcome": "unknown",
                    "detail": "worker returned no probe metrics for assigned stream",
                    "durationMs": 0,
                }
            ]
        seen_checks = set()
        for metric in sorted(stream_metrics, key=lambda item: str(item.get("check", ""))):
            check = str(metric.get("check", ""))
            if not check or len(check) > MAX_IDENTIFIER_LENGTH or check in seen_checks:
                raise WorkerReportValidationError(
                    f"probe metric checks for {stream_id} must be unique and bounded"
                )
            seen_checks.add(check)
            outcome = str(metric.get("outcome", "unknown"))
            status = status_by_outcome.get(outcome, "unknown")
            result_id = uuid.uuid5(report_id, f"{stream_id}:probe.{check}")
            epoch, config_version, lease_sequence = lease_by_stream[stream_id]
            result = CheckResult(
                result_id=result_id,
                stream_id=stream_id,
                check_id=f"probe.{check}",
                lease_epoch=epoch,
                config_version=config_version,
                sequence=lease_sequence,
                status=status,
                observed_at=observed_at,
                evidence={
                    "outcome": outcome,
                    "durationMs": metric.get("durationMs", 0),
                    "message": str(metric.get("detail", outcome))[:200],
                    "protocol": metric.get("protocol", "unknown"),
                    "source": metric.get("source", "unknown"),
                },
            )
            results.append(result)
            result_streams[str(result_id)] = stream_id
        for observation in sorted(
            observations_by_stream[stream_id], key=lambda item: item["monitorId"]
        ):
            monitor_id = observation["monitorId"]
            result_id = uuid.uuid5(report_id, f"{stream_id}:monitor:{monitor_id}")
            epoch, config_version, lease_sequence = lease_by_stream[stream_id]
            result = CheckResult(
                result_id=result_id,
                stream_id=stream_id,
                check_id=monitor_id,
                lease_epoch=epoch,
                config_version=config_version,
                sequence=lease_sequence,
                status=observation["status"],
                observed_at=observed_at,
                evidence={
                    "message": observation["message"],
                    "source": "monitor_observation",
                },
            )
            results.append(result)
            result_streams[str(result_id)] = stream_id

    disposition = store.ingest_report(
        FencedReport(
            report_id=report_id,
            tenant_id=store.tenant_id,
            worker_id=worker_id,
            worker_incarnation_id=incarnation,
            results=tuple(results),
        )
    )
    rejected_stream_ids = {
        result_streams[item["resultId"]]
        for item in disposition.rejected
        if item.get("resultId") in result_streams
    }
    accepted_result_ids = set(disposition.accepted_result_ids) | set(
        disposition.duplicate_result_ids
    )
    accepted_stream_ids = {
        result_streams[result_id]
        for result_id in accepted_result_ids
        if result_id in result_streams
    } - rejected_stream_ids
    projection_fences = {
        stream_id: (
            lease_by_stream[stream_id][0],
            lease_by_stream[stream_id][1],
            lease_by_stream[stream_id][2],
        )
        for stream_id in accepted_stream_ids
    }
    dropped_items = dropped_claimed
    # PostgreSQL commits catalog monitor observations and their direct read
    # projection in the fenced ingestion transaction. The legacy JSON shadow
    # is deliberately not a durable-mode retry target.
    projected_stream_ids = set() if disposition.duplicate else set(projection_fences)
    conflict = bool(disposition.rejected)
    return {
        "ok": not conflict,
        "apiVersion": WORKER_API_VERSION_V2,
        "workerId": worker_id,
        "workerIncarnationId": str(incarnation),
        "reportId": str(report_id),
        "streamIds": sorted(accepted_stream_ids),
        "projectedStreamIds": sorted(projected_stream_ids),
        "rejectedStreamIds": sorted(claimed_stream_ids - accepted_stream_ids),
        "droppedItems": {
            key: sorted(
                set(dropped_claimed.get(key, []))
                | set(dropped_items.get(key, []))
            )
            for key in dropped_items
        },
        "disposition": disposition.payload(),
        "retryAssignment": conflict,
    }


def persist_worker_projection(
    state: GuiState,
    worker_id: str,
    accepted_stream_ids: set[str],
    scoped_state: dict,
):
    with state.control_plane_lock:
        with state.monitor_state_lock:
            path = monitor_state_path(state)
            payload = load_monitor_payload(path)
            if accepted_stream_ids:
                payload["alarms"] = [
                    alarm
                    for alarm in payload.get("alarms", [])
                    if alarm.get("streamId") not in accepted_stream_ids
                ]
                payload["events"] = [
                    event
                    for event in payload.get("events", [])
                    if event.get("streamId") not in accepted_stream_ids
                ]
                payload["pending"] = [
                    item
                    for item in payload.get("pending", [])
                    if item.get("streamId") not in accepted_stream_ids
                ]
            payload["alarms"].extend(
                with_worker(item, worker_id) for item in scoped_state["alarms"]
            )
            payload["events"].extend(
                with_worker(item, worker_id) for item in scoped_state["events"]
            )
            payload["pending"].extend(
                with_worker(item, worker_id) for item in scoped_state["pending"]
            )
            payload["updatedAt"] = scoped_state.get(
                "updatedAt", payload.get("updatedAt", "")
            )
            payload["monitors"] = monitor_catalog_payload()
            payload["workers"] = worker_status_payload(state)
            active_worker_ids_set = set(state.worker_seen)
            payload["workerProbeMetrics"] = {
                metric_worker_id: metrics
                for metric_worker_id, metrics in payload.get(
                    "workerProbeMetrics", {}
                ).items()
                if metric_worker_id in active_worker_ids_set
            }
            if "probeMetrics" in scoped_state:
                payload["workerProbeMetrics"][worker_id] = scoped_state[
                    "probeMetrics"
                ]
            if not write_monitor_payload(path, payload):
                raise WorkerReportPersistenceError("monitor state persistence failed")
        state.worker_seen[worker_id] = time.time()


def apply_worker_report(
    state: GuiState,
    worker_id: str,
    stream_ids: list[str],
    worker_state: dict,
    report_contract: dict | None = None,
) -> dict:
    if not isinstance(stream_ids, list) or any(not isinstance(stream_id, str) or not stream_id for stream_id in stream_ids):
        raise WorkerReportValidationError("streamIds must be an array of non-empty strings")
    if len(stream_ids) > MAX_REPORT_STREAMS:
        raise WorkerReportValidationError(f"streamIds exceeds {MAX_REPORT_STREAMS} items")
    if any(len(stream_id) > MAX_IDENTIFIER_LENGTH for stream_id in stream_ids):
        raise WorkerReportValidationError("streamIds contains an identifier that is too long")
    claimed_stream_ids = set(stream_ids)
    report_contract = report_contract or {}

    with state.control_plane_lock:
        current_contract, _ = current_assignment_contract(state, worker_id)
        legacy_contract = validate_report_contract(
            current_contract,
            report_contract,
            allow_legacy=state.allow_legacy_worker_reports,
        )
        authoritative_stream_ids = set(current_contract.stream_ids)
        accepted_stream_ids = claimed_stream_ids & authoritative_stream_ids
        rejected_stream_ids = claimed_stream_ids - authoritative_stream_ids
        scoped_state, dropped_items = validate_monitor_items(worker_state, accepted_stream_ids)

        persist_worker_projection(
            state,
            worker_id,
            accepted_stream_ids,
            scoped_state,
        )

    return {
        "ok": True,
        "workerId": worker_id,
        "streamIds": sorted(accepted_stream_ids),
        "rejectedStreamIds": sorted(rejected_stream_ids),
        "droppedItems": dropped_items,
        "legacyContract": legacy_contract,
        **current_contract.payload(),
    }


def with_worker(item: dict, worker_id: str) -> dict:
    copy = dict(item)
    copy["workerId"] = worker_id
    return copy


def load_monitor_payload(path: Path) -> dict:
    if not path.is_file():
        return {
            "updatedAt": "",
            "alarms": [],
            "events": [],
            "pending": [],
            "monitors": monitor_catalog_payload(),
            "workers": [],
            "workerProbeMetrics": {},
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "updatedAt": "",
            "alarms": [],
            "events": [],
            "pending": [],
            "monitors": monitor_catalog_payload(),
            "workers": [],
            "workerProbeMetrics": {},
        }
    payload.setdefault("alarms", [])
    payload.setdefault("events", [])
    payload.setdefault("pending", [])
    payload.setdefault("workers", [])
    payload.setdefault("workerProbeMetrics", {})
    return payload


def write_monitor_payload(path: Path, payload: dict) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temp_path.replace(path)
    except OSError:
        return False
    return True


def monitor_state_path(state: GuiState) -> Path:
    return Path(state.monitor_state_path or os.environ.get("VIDEOSIM_MONITOR_STATE", DEFAULT_MONITOR_STATE_PATH))


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
