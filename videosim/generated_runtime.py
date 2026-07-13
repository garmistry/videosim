from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass

from .control_plane import MAX_IDENTIFIER_LENGTH
from .feed_store import configured_database_url
from .framerate import normalize_frame_rate
from .gui import PROFILE_OPTIONS, PROTOCOL_OPTIONS, profile_for


@dataclass(frozen=True)
class RuntimeFeed:
    id: str
    config_version: int
    protocol: str
    mode: str
    feed_port: int
    width: int
    height: int
    framerate: str


@dataclass
class OwnedFeed:
    feed: RuntimeFeed
    process: subprocess.Popen
    lock_id: int
    port_lock_id: int | None = None


def runtime_lock_id(tenant_id: str, feed_id: str) -> int:
    value = hashlib.sha256(
        f"videosim.generated-runtime:{tenant_id}:{feed_id}".encode()
    ).digest()[:8]
    return int.from_bytes(value, "big", signed=True)


def runtime_port_lock_id(tenant_id: str, port: int) -> int:
    value = hashlib.sha256(
        f"videosim.generated-runtime-port:{tenant_id}:{port}".encode()
    ).digest()[:8]
    return int.from_bytes(value, "big", signed=True)


def parse_runtime_feed(row: dict, port_start: int, port_end: int) -> RuntimeFeed:
    config = dict(row["config"])
    feed_id = row["id"]
    if (
        not isinstance(feed_id, str)
        or not feed_id
        or len(feed_id) > MAX_IDENTIFIER_LENGTH
        or Path(feed_id).name != feed_id
    ):
        raise ValueError("generated feed ID must be one safe path segment")
    if config.get("id") != feed_id:
        raise ValueError(f"generated feed {feed_id} has a mismatched config ID")
    if config.get("source") != "generated" or config.get("desired_state") != "running":
        raise ValueError(f"generated feed {feed_id} is not desired running")
    protocol = config.get("protocol")
    mode = config.get("mode")
    if protocol not in PROTOCOL_OPTIONS or mode not in PROFILE_OPTIONS:
        raise ValueError(f"generated feed {feed_id} has an unsupported profile")
    feed_port = config.get("feed_port")
    width = config.get("width")
    height = config.get("height")
    if type(feed_port) is not int or not 1 <= feed_port <= 65535:
        raise ValueError(f"generated feed {feed_id} has an invalid port")
    if protocol == "srt" and not port_start <= feed_port <= port_end:
        raise ValueError(
            f"generated feed {feed_id} port {feed_port} is outside "
            f"{port_start}-{port_end}"
        )
    if type(width) is not int or type(height) is not int or min(width, height) < 1:
        raise ValueError(f"generated feed {feed_id} has invalid dimensions")
    config_version = row["config_version"]
    if type(config_version) is not int or config_version < 1:
        raise ValueError(f"generated feed {feed_id} has an invalid config version")
    return RuntimeFeed(
        id=feed_id,
        config_version=config_version,
        protocol=protocol,
        mode=mode,
        feed_port=feed_port,
        width=width,
        height=height,
        framerate=normalize_frame_rate(config.get("framerate")),
    )


class GeneratedFeedRuntime:
    def __init__(
        self,
        connection,
        *,
        tenant_id: str = "default",
        runtime_id: str = "",
        max_feeds: int = 100,
        srt_port_start: int = 9000,
        srt_port_end: int = 9999,
        dash_root: str | Path = "/tmp/videosim-dash-origin",
        dash_base_url: str = "http://127.0.0.1:8090",
        process_factory=subprocess.Popen,
    ):
        if not tenant_id or len(tenant_id) > 512:
            raise ValueError("tenant_id must be a bounded non-empty string")
        if max_feeds < 1:
            raise ValueError("max_feeds must be positive")
        if not 1 <= srt_port_start <= srt_port_end <= 65535:
            raise ValueError("invalid SRT port range")
        if not dash_base_url.startswith(("http://", "https://")):
            raise ValueError("dash_base_url must be an HTTP(S) URL")
        self.connection = connection
        self.connection.autocommit = True
        self.tenant_id = tenant_id
        self.runtime_id = runtime_id or f"{socket.gethostname()}-{os.getpid()}"
        self.max_feeds = max_feeds
        self.srt_port_start = srt_port_start
        self.srt_port_end = srt_port_end
        self.dash_root = Path(dash_root)
        self.dash_base_url = dash_base_url.rstrip("/")
        self.process_factory = process_factory
        self.owned: dict[str, OwnedFeed] = {}

    def log(self, message: str):
        print(
            f"[videosim-generated-runtime] runtime={self.runtime_id} {message}",
            flush=True,
        )

    def _desired_rows(self) -> list[dict]:
        return self.connection.execute(
            """
            SELECT id, config, config_version
            FROM feeds
            WHERE tenant_id = %s
              AND config ->> 'source' = 'generated'
              AND config ->> 'desired_state' = 'running'
            ORDER BY id
            """,
            (self.tenant_id,),
        ).fetchall()

    def _current_row(self, feed_id: str) -> dict | None:
        return self.connection.execute(
            """
            SELECT id, config, config_version
            FROM feeds
            WHERE tenant_id = %s AND id = %s
            """,
            (self.tenant_id, feed_id),
        ).fetchone()

    def _try_lock(self, feed_id: str) -> int | None:
        lock_id = runtime_lock_id(self.tenant_id, feed_id)
        row = self.connection.execute(
            "SELECT pg_try_advisory_lock(%s) AS locked", (lock_id,)
        ).fetchone()
        return lock_id if row["locked"] else None

    def _try_port_lock(self, port: int) -> int | None:
        lock_id = runtime_port_lock_id(self.tenant_id, port)
        row = self.connection.execute(
            "SELECT pg_try_advisory_lock(%s) AS locked", (lock_id,)
        ).fetchone()
        return lock_id if row["locked"] else None

    def _unlock(self, lock_id: int):
        row = self.connection.execute(
            "SELECT pg_advisory_unlock(%s) AS unlocked", (lock_id,)
        ).fetchone()
        if not row["unlocked"]:
            raise RuntimeError("generated runtime advisory lock was not held")

    def command_for(self, feed: RuntimeFeed) -> list[str]:
        command = [
            sys.executable,
            "-m",
            "videosim",
            "start",
            "--profile",
            profile_for(feed.protocol, feed.mode),
            "--port",
            str(feed.feed_port),
            "--width",
            str(feed.width),
            "--height",
            str(feed.height),
            "--framerate",
            feed.framerate,
        ]
        if feed.protocol == "dash":
            dash_dir = self.dash_root / "dash" / feed.id
            dash_dir.mkdir(parents=True, exist_ok=True)
            command.extend(
                [
                    "--protocol",
                    "dash",
                    "--dash-dir",
                    str(dash_dir),
                    "--dash-base-url",
                    f"{self.dash_base_url}/dash/{feed.id}",
                ]
            )
        return command

    def _stop(self, owned: OwnedFeed, *, unlock: bool) -> bool:
        process = owned.process
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGINT)
                process.wait(timeout=10)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                except ProcessLookupError:
                    pass
                except (OSError, subprocess.TimeoutExpired) as exc:
                    self.log(f"feed={owned.feed.id} cleanup_failed={exc}")
                    return False
            except OSError as exc:
                self.log(f"feed={owned.feed.id} cleanup_failed={exc}")
                return False
        if unlock:
            if owned.port_lock_id is not None:
                self._unlock(owned.port_lock_id)
            self._unlock(owned.lock_id)
        self.log(f"feed={owned.feed.id} stopped")
        return True

    def reconcile(self) -> dict:
        desired: dict[str, RuntimeFeed] = {}
        for row in self._desired_rows():
            try:
                feed = parse_runtime_feed(
                    row, self.srt_port_start, self.srt_port_end
                )
            except ValueError as exc:
                self.log(f"rejected={exc}")
                continue
            desired[feed.id] = feed

        for feed_id, owned in list(self.owned.items()):
            current = desired.get(feed_id)
            if (
                current != owned.feed
                or owned.process.poll() is not None
            ):
                if self._stop(owned, unlock=True):
                    self.owned.pop(feed_id, None)

        used_srt_ports = {
            owned.feed.feed_port
            for owned in self.owned.values()
            if owned.feed.protocol == "srt"
        }
        for feed_id, offered in desired.items():
            if feed_id in self.owned or len(self.owned) >= self.max_feeds:
                continue
            if offered.protocol == "srt" and offered.feed_port in used_srt_ports:
                self.log(
                    f"feed={feed_id} rejected=duplicate_srt_port "
                    f"port={offered.feed_port}"
                )
                continue
            lock_id = self._try_lock(feed_id)
            if lock_id is None:
                continue
            port_lock_id = None
            row = self._current_row(feed_id)
            try:
                current = (
                    parse_runtime_feed(
                        row, self.srt_port_start, self.srt_port_end
                    )
                    if row is not None
                    else None
                )
            except ValueError as exc:
                self._unlock(lock_id)
                self.log(f"feed={feed_id} rejected={exc}")
                continue
            if current != offered:
                self._unlock(lock_id)
                continue
            if current.protocol == "srt":
                port_lock_id = self._try_port_lock(current.feed_port)
                if port_lock_id is None:
                    self._unlock(lock_id)
                    self.log(
                        f"feed={feed_id} rejected=claimed_srt_port "
                        f"port={current.feed_port}"
                    )
                    continue
            try:
                process = self.process_factory(
                    self.command_for(current), start_new_session=True
                )
            except OSError as exc:
                if port_lock_id is not None:
                    self._unlock(port_lock_id)
                self._unlock(lock_id)
                self.log(f"feed={feed_id} launch_failed={exc}")
                continue
            self.owned[feed_id] = OwnedFeed(
                current, process, lock_id, port_lock_id
            )
            if current.protocol == "srt":
                used_srt_ports.add(current.feed_port)
            self.log(
                f"feed={feed_id} version={current.config_version} "
                f"protocol={current.protocol} pid={process.pid} started"
            )
        return {
            "desired": len(desired),
            "owned": len(self.owned),
            "capacity": self.max_feeds,
        }

    def close(self, *, connection_healthy: bool = True):
        for feed_id, owned in list(self.owned.items()):
            if self._stop(owned, unlock=connection_healthy):
                self.owned.pop(feed_id, None)
        self.connection.close()

    def run(self, poll_seconds: float = 2.0) -> int:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        stopping = False

        def stop(_signum, _frame):
            nonlocal stopping
            stopping = True

        previous = {
            signal_number: signal.signal(signal_number, stop)
            for signal_number in (signal.SIGINT, signal.SIGTERM)
        }
        healthy = True
        try:
            while not stopping:
                self.reconcile()
                time.sleep(poll_seconds)
        except Exception as exc:
            healthy = False
            self.log(f"fatal={exc}")
            return 1
        finally:
            self.close(connection_healthy=healthy)
            for signal_number, handler in previous.items():
                signal.signal(signal_number, handler)
        return 0


def connect(database_url: str):
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - deployment dependency path.
        raise RuntimeError("generated runtime requires requirements.txt") from exc
    return psycopg.connect(database_url, row_factory=dict_row)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run durable generated feeds")
    parser.add_argument("--database-url", default="")
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--runtime-id", default="")
    parser.add_argument("--max-feeds", type=int, default=100)
    parser.add_argument("--srt-port-start", type=int, default=9000)
    parser.add_argument("--srt-port-end", type=int, default=9999)
    parser.add_argument("--dash-root", default="/tmp/videosim-dash-origin")
    parser.add_argument("--dash-base-url", default="http://127.0.0.1:8090")
    parser.add_argument("--poll-interval-seconds", type=float, default=2)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    database_url = args.database_url or configured_database_url()
    if not database_url:
        raise SystemExit("PostgreSQL database URL is required")
    runtime = GeneratedFeedRuntime(
        connect(database_url),
        tenant_id=args.tenant_id,
        runtime_id=args.runtime_id,
        max_feeds=args.max_feeds,
        srt_port_start=args.srt_port_start,
        srt_port_end=args.srt_port_end,
        dash_root=args.dash_root,
        dash_base_url=args.dash_base_url,
    )
    return runtime.run(args.poll_interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
