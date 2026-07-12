from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Mapping, Protocol


class FeedRegistrationStore(Protocol):
    def load(self) -> list[dict]:
        ...

    def upsert(self, feed: Mapping) -> None:
        ...

    def delete(self, feed_id: str) -> None:
        ...


def configured_database_url() -> str:
    return os.environ.get("VIDEOSIM_DATABASE_URL", "").strip()


def default_feed_store() -> FeedRegistrationStore:
    database_url = configured_database_url()
    if database_url:
        from .postgres_store import PostgresControlPlaneStore

        return PostgresControlPlaneStore(
            database_url,
            min_pool_size=int(os.environ.get("VIDEOSIM_DB_POOL_MIN", "1")),
            max_pool_size=int(os.environ.get("VIDEOSIM_DB_POOL_MAX", "10")),
            alarm_repeat_seconds=int(os.environ.get("VIDEOSIM_ALARM_REPEAT_SECONDS", "5")),
            alarm_event_history_limit=int(os.environ.get("VIDEOSIM_ALARM_EVENT_HISTORY_LIMIT", "1000")),
            alarm_event_retention_seconds=int(
                os.environ.get("VIDEOSIM_ALARM_EVENT_RETENTION_SECONDS", str(7 * 24 * 60 * 60))
            ),
        )
    return SqliteFeedStore(default_feed_db_path())


def default_feed_db_path() -> Path:
    configured = os.environ.get("VIDEOSIM_DB_PATH")
    if configured:
        return Path(configured).expanduser()
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")).expanduser()
    return data_home / "videosim" / "feeds.sqlite3"


class SqliteFeedStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._closed = False
        self._migrate()

    def load(self) -> list[dict]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT id, name, source, external_url, protocol, mode, http_port, feed_port,
                       width, height, framerate, dash_dir, alert_enabled_ids, alert_delay_seconds
                FROM feeds
                ORDER BY created_at, id
                """
            ).fetchall()
        return [self._row_to_feed(row) for row in rows]

    def upsert(self, feed: Mapping) -> None:
        values = {
            "id": feed["id"],
            "name": feed["name"],
            "source": feed.get("source", "generated"),
            "external_url": feed.get("external_url", ""),
            "protocol": feed["protocol"],
            "mode": feed["mode"],
            "http_port": int(feed["http_port"]),
            "feed_port": int(feed["feed_port"]),
            "width": int(feed["width"]),
            "height": int(feed["height"]),
            "framerate": str(feed["framerate"]),
            "dash_dir": feed["dash_dir"],
            "alert_enabled_ids": json.dumps(feed.get("alert_enabled_ids")) if feed.get("alert_enabled_ids") is not None else None,
            "alert_delay_seconds": int(feed.get("alert_delay_seconds", 0)),
        }
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO feeds (
                  id, name, source, external_url, protocol, mode, http_port, feed_port,
                  width, height, framerate, dash_dir, alert_enabled_ids, alert_delay_seconds
                ) VALUES (
                  :id, :name, :source, :external_url, :protocol, :mode, :http_port, :feed_port,
                  :width, :height, :framerate, :dash_dir, :alert_enabled_ids, :alert_delay_seconds
                )
                ON CONFLICT(id) DO UPDATE SET
                  name = excluded.name,
                  source = excluded.source,
                  external_url = excluded.external_url,
                  protocol = excluded.protocol,
                  mode = excluded.mode,
                  http_port = excluded.http_port,
                  feed_port = excluded.feed_port,
                  width = excluded.width,
                  height = excluded.height,
                  framerate = excluded.framerate,
                  dash_dir = excluded.dash_dir,
                  alert_enabled_ids = excluded.alert_enabled_ids,
                  alert_delay_seconds = excluded.alert_delay_seconds,
                  updated_at = CURRENT_TIMESTAMP
                """,
                values,
            )
            self._connection.commit()

    def delete(self, feed_id: str) -> None:
        with self._lock:
            self._connection.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))
            self._connection.commit()

    def close(self) -> None:
        with getattr(self, "_lock", threading.RLock()):
            if not getattr(self, "_closed", True):
                self._connection.close()
                self._closed = True

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def _migrate(self) -> None:
        with self._lock:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS feeds (
                  id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  source TEXT NOT NULL DEFAULT 'generated',
                  external_url TEXT NOT NULL DEFAULT '',
                  protocol TEXT NOT NULL,
                  mode TEXT NOT NULL,
                  http_port INTEGER NOT NULL,
                  feed_port INTEGER NOT NULL,
                  width INTEGER NOT NULL,
                  height INTEGER NOT NULL,
                  framerate TEXT NOT NULL,
                  dash_dir TEXT NOT NULL,
                  alert_enabled_ids TEXT,
                  alert_delay_seconds INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self._connection.commit()

    @staticmethod
    def _row_to_feed(row: sqlite3.Row) -> dict:
        enabled = row["alert_enabled_ids"]
        return {
            "id": row["id"],
            "name": row["name"],
            "source": row["source"],
            "external_url": row["external_url"],
            "protocol": row["protocol"],
            "mode": row["mode"],
            "http_port": row["http_port"],
            "feed_port": row["feed_port"],
            "width": row["width"],
            "height": row["height"],
            "framerate": row["framerate"],
            "dash_dir": row["dash_dir"],
            "alert_enabled_ids": json.loads(enabled) if enabled else None,
            "alert_delay_seconds": row["alert_delay_seconds"],
        }
