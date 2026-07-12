from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import time
import uuid
from dataclasses import dataclass
from typing import Iterable

from .postgres_store import PostgresControlPlaneStore, PostgresStoreError


STREAM_NAME = "VIDEOSIM_EVENTS"
STREAM_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
STREAM_DUPLICATE_WINDOW_SECONDS = 2 * 60
STREAM_SUBJECTS = (
    "videosim.results.accepted.v1",
    "videosim.alarms.transition.v1",
    "videosim.audit.v1",
    "videosim.workers.health.v1",
)


class NatsPublisherError(RuntimeError):
    pass


@dataclass(frozen=True)
class PublishSummary:
    claimed: int
    published: int
    failed: int


def _nats_modules():
    try:
        import nats
        from nats.js.errors import NotFoundError
    except ImportError as exc:  # pragma: no cover - deployment dependency path.
        raise NatsPublisherError("NATS support requires requirements.txt") from exc
    return nats, NotFoundError


async def ensure_event_stream(nats_url: str):
    nats, NotFoundError = _nats_modules()
    client = await nats.connect(servers=[nats_url], connect_timeout=5)
    try:
        jetstream = client.jetstream()
        try:
            info = await jetstream.stream_info(STREAM_NAME)
        except NotFoundError:
            info = await jetstream.add_stream(
                name=STREAM_NAME,
                subjects=list(STREAM_SUBJECTS),
                storage="file",
                retention="limits",
                max_age=STREAM_MAX_AGE_SECONDS,
                duplicate_window=STREAM_DUPLICATE_WINDOW_SECONDS,
            )
        validate_event_stream_config(info.config)
    finally:
        await client.drain()


def validate_event_stream_config(config):
    storage = getattr(config.storage, "value", str(config.storage)).lower()
    retention = getattr(config.retention, "value", str(config.retention)).lower()
    subjects = set(config.subjects or [])
    mismatches = []
    if storage != "file":
        mismatches.append(f"storage={storage}")
    if retention != "limits":
        mismatches.append(f"retention={retention}")
    if subjects != set(STREAM_SUBJECTS):
        mismatches.append(
            f"subjects={sorted(subjects)} expected={sorted(STREAM_SUBJECTS)}"
        )
    if abs(float(config.max_age or 0) - STREAM_MAX_AGE_SECONDS) > 0.001:
        mismatches.append(f"maxAge={config.max_age}")
    if abs(float(config.duplicate_window or 0) - STREAM_DUPLICATE_WINDOW_SECONDS) > 0.001:
        mismatches.append(f"duplicateWindow={config.duplicate_window}")
    if mismatches:
        raise NatsPublisherError(
            f"JetStream {STREAM_NAME} configuration mismatch: {', '.join(mismatches)}"
        )


async def publish_outbox_once(
    store: PostgresControlPlaneStore,
    nats_url: str,
    *,
    publisher_id: str,
    limit: int = 100,
    event_ids: Iterable[uuid.UUID] | None = None,
) -> PublishSummary:
    nats, _ = _nats_modules()
    records = store.claim_outbox(publisher_id, limit, event_ids=event_ids)
    if not records:
        return PublishSummary(0, 0, 0)
    try:
        client = await nats.connect(servers=[nats_url], connect_timeout=5, max_reconnect_attempts=0)
    except Exception as exc:
        for record in records:
            store.mark_outbox_failed(record.id, publisher_id, str(exc))
        return PublishSummary(len(records), 0, len(records))
    published = 0
    failed = 0
    try:
        jetstream = client.jetstream()
        for record in records:
            try:
                encoded = json.dumps(
                    record.payload, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
                if hashlib.sha256(encoded).hexdigest() != record.payload_sha256:
                    raise NatsPublisherError("outbox payload hash mismatch")
                ack = await jetstream.publish(
                    record.subject,
                    encoded,
                    headers={"Nats-Msg-Id": str(record.event_id)},
                )
                store.mark_outbox_published(record.id, publisher_id, int(ack.seq))
                published += 1
            except Exception as exc:
                try:
                    store.mark_outbox_failed(record.id, publisher_id, str(exc))
                except PostgresStoreError:
                    # A stale publisher must not overwrite a claim already
                    # reclaimed by another publisher.
                    pass
                failed += 1
    finally:
        await client.drain()
    return PublishSummary(len(records), published, failed)


def run_outbox_publisher(
    database_url: str,
    nats_url: str,
    *,
    once: bool = False,
    poll_seconds: float = 1.0,
    batch_size: int = 100,
    publisher_id: str = "",
) -> int:
    if poll_seconds <= 0:
        raise ValueError("poll_seconds must be greater than 0")
    publisher_id = publisher_id or f"{socket.gethostname()}-{uuid.uuid4()}"
    store = PostgresControlPlaneStore(database_url)
    try:
        while True:
            summary = asyncio.run(
                publish_outbox_once(store, nats_url, publisher_id=publisher_id, limit=batch_size)
            )
            if once:
                return 0 if summary.failed == 0 else 1
            if summary.claimed == 0:
                time.sleep(poll_seconds)
    finally:
        store.close()
