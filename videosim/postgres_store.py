from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping

from .monitor_catalog import SPEC_BY_ID


DEFAULT_TENANT_ID = "default"
DEFAULT_ALARM_REPEAT_SECONDS = 5
DEFAULT_ALARM_EVENT_HISTORY_LIMIT = 1000
DEFAULT_ALARM_EVENT_RETENTION_SECONDS = 7 * 24 * 60 * 60

# Keep the durable policy independent of GUI runtime objects. These controls
# mirror feed-mode expectations so an explicit config change can suppress an
# alarm that is no longer applicable without treating missing probe evidence as
# a recovery.
_MODE_MONITOR_CONTROLS = {
    "normal": {"video": True, "audio": True, "captions": True, "black": False, "frozen": False},
    "audio_only": {"video": False, "audio": True, "captions": False, "black": False, "frozen": False},
    "video_only": {"video": True, "audio": False, "captions": True, "black": False, "frozen": False},
    "no_captions": {"video": True, "audio": True, "captions": False, "black": False, "frozen": False},
    "black_video": {"video": True, "audio": True, "captions": True, "black": True, "frozen": False},
    "frozen_video": {"video": True, "audio": True, "captions": True, "black": False, "frozen": True},
}


class PostgresStoreError(RuntimeError):
    pass


class LeaseConflict(PostgresStoreError):
    pass


class ReportConflict(PostgresStoreError):
    pass


@dataclass(frozen=True)
class DurableLease:
    tenant_id: str
    stream_id: str
    worker_id: str
    worker_incarnation_id: uuid.UUID
    epoch: int
    config_version: int
    expires_at: datetime
    state: str


@dataclass(frozen=True)
class CheckResult:
    result_id: uuid.UUID
    stream_id: str
    check_id: str
    lease_epoch: int
    config_version: int
    sequence: int
    status: str
    observed_at: datetime
    evidence: Mapping

    def payload(self) -> dict:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return {
            "resultId": str(self.result_id),
            "streamId": self.stream_id,
            "checkId": self.check_id,
            "leaseEpoch": self.lease_epoch,
            "configVersion": self.config_version,
            "sequence": self.sequence,
            "status": self.status,
            "observedAt": self.observed_at.astimezone(timezone.utc).isoformat(),
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class FencedReport:
    report_id: uuid.UUID
    tenant_id: str
    worker_id: str
    worker_incarnation_id: uuid.UUID
    results: tuple[CheckResult, ...]
    projection_sha256: str = ""

    def payload(self) -> dict:
        return {
            "reportId": str(self.report_id),
            "tenantId": self.tenant_id,
            "workerId": self.worker_id,
            "workerIncarnationId": str(self.worker_incarnation_id),
            "projectionSha256": self.projection_sha256,
            "results": [result.payload() for result in self.results],
        }


@dataclass(frozen=True)
class OutboxRecord:
    id: int
    event_id: uuid.UUID
    subject: str
    payload: dict
    payload_sha256: str
    attempts: int


@dataclass(frozen=True)
class IngestDisposition:
    report_id: uuid.UUID
    duplicate: bool
    accepted_result_ids: tuple[str, ...]
    duplicate_result_ids: tuple[str, ...]
    rejected: tuple[dict, ...]

    def payload(self) -> dict:
        return {
            "reportId": str(self.report_id),
            "duplicate": self.duplicate,
            "acceptedResultIds": list(self.accepted_result_ids),
            "duplicateResultIds": list(self.duplicate_result_ids),
            "rejected": list(self.rejected),
        }


def _postgres_modules():
    try:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
    except ImportError as exc:  # pragma: no cover - deployment dependency path.
        raise PostgresStoreError("PostgreSQL support requires requirements.txt") from exc
    return ConnectionPool, dict_row


class PostgresControlPlaneStore:
    """Bounded-pool PostgreSQL authority for the production control plane."""

    def __init__(
        self,
        database_url: str,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        min_pool_size: int = 1,
        max_pool_size: int = 10,
        worker_freshness_seconds: int = 60,
        max_future_skew_seconds: int = 30,
        result_freshness_seconds: int = 120,
        alarm_repeat_seconds: int = DEFAULT_ALARM_REPEAT_SECONDS,
        alarm_event_history_limit: int = DEFAULT_ALARM_EVENT_HISTORY_LIMIT,
        alarm_event_retention_seconds: int = DEFAULT_ALARM_EVENT_RETENTION_SECONDS,
    ):
        if not database_url:
            raise ValueError("database_url is required")
        if min_pool_size < 0 or max_pool_size < 1 or min_pool_size > max_pool_size:
            raise ValueError("invalid PostgreSQL pool size")
        if min(worker_freshness_seconds, max_future_skew_seconds, result_freshness_seconds) <= 0:
            raise ValueError("freshness and clock-skew limits must be greater than 0")
        if min(alarm_repeat_seconds, alarm_event_history_limit, alarm_event_retention_seconds) <= 0:
            raise ValueError("alarm repeat and retention limits must be greater than 0")
        ConnectionPool, dict_row = _postgres_modules()
        self.database_url = database_url
        self.tenant_id = tenant_id
        self.worker_freshness_seconds = worker_freshness_seconds
        self.max_future_skew_seconds = max_future_skew_seconds
        self.result_freshness_seconds = result_freshness_seconds
        self.alarm_repeat_seconds = alarm_repeat_seconds
        self.alarm_event_history_limit = alarm_event_history_limit
        self.alarm_event_retention_seconds = alarm_event_retention_seconds
        self._pool = ConnectionPool(
            conninfo=database_url,
            min_size=min_pool_size,
            max_size=max_pool_size,
            kwargs={"row_factory": dict_row},
            open=True,
        )

    def close(self):
        self._pool.close()

    @contextmanager
    def assignment_scheduler_transaction(self):
        lock_id = int.from_bytes(
            hashlib.sha256(f"videosim.scheduler:{self.tenant_id}".encode()).digest()[:8],
            "big",
            signed=True,
        )
        with self._pool.connection() as connection:
            with connection.transaction():
                # ponytail: one transaction per tenant; shard after measured contention.
                acquired = connection.execute(
                    "SELECT pg_try_advisory_xact_lock(%s) AS acquired",
                    (lock_id,),
                ).fetchone()["acquired"]
                if not acquired:
                    raise PostgresStoreError(
                        "another scheduler transaction is active for this tenant"
                    )
                yield connection

    # FeedRegistrationStore compatibility.
    def load(self) -> list[dict]:
        with self._pool.connection() as connection:
            rows = connection.execute(
                "SELECT config, config_version FROM feeds WHERE tenant_id = %s ORDER BY created_at, id",
                (self.tenant_id,),
            ).fetchall()
        feeds = []
        for row in rows:
            config = dict(row["config"])
            config["config_version"] = int(row["config_version"])
            feeds.append(config)
        return feeds

    def upsert(self, feed: Mapping) -> int:
        return self.import_feed_if_changed(feed)[1]

    def upsert_versioned(self, feed: Mapping, expected_version: int | None) -> int:
        if expected_version is None:
            return self.upsert(feed)
        config = dict(feed)
        config.pop("config_version", None)
        with self._pool.connection() as connection:
            with connection.transaction():
                _, version = self._upsert_feed_with_expected_version_in_transaction(
                    connection, config, expected_version
                )
                return version

    def import_feed_if_changed(self, feed: Mapping) -> tuple[bool, int]:
        config = dict(feed)
        config.pop("config_version", None)
        with self._pool.connection() as connection:
            with connection.transaction():
                return self._upsert_feed_in_transaction(connection, config)

    def upsert_with_audit(
        self,
        feed: Mapping,
        audit: Mapping,
        *,
        expected_version: int | None = None,
    ) -> int:
        """Commit one feed create/update and its success audit atomically.

        A GUI-loaded feed supplies its durable configuration version. Version
        zero means a new locally allocated feed ID and may only insert; a
        positive version may only update that exact durable row. A retained
        generation makes delete/recreate ABA attempts fail as well.
        """
        config = dict(feed)
        config.pop("config_version", None)
        with self._pool.connection() as connection:
            with connection.transaction():
                if expected_version is None:
                    _, version = self._upsert_feed_in_transaction(connection, config)
                else:
                    _, version = self._upsert_feed_with_expected_version_in_transaction(
                        connection, config, expected_version
                    )
                self._append_mutation_audit(
                    connection, audit, resource_type="feed", resource_id=str(feed["id"])
                )
                return version

    def _lock_feed_generation(self, connection, feed_id: str) -> int | None:
        row = connection.execute(
            """
            SELECT config_version FROM feed_generations
            WHERE tenant_id = %s AND id = %s
            FOR UPDATE
            """,
            (self.tenant_id, feed_id),
        ).fetchone()
        return int(row["config_version"]) if row is not None else None

    def _lock_or_create_feed_generation(
        self, connection, feed_id: str
    ) -> tuple[int, bool]:
        version = self._lock_feed_generation(connection, feed_id)
        if version is not None:
            return version, False
        row = connection.execute(
            """
            INSERT INTO feed_generations (tenant_id, id, config_version)
            VALUES (%s, %s, 1)
            ON CONFLICT (tenant_id, id) DO NOTHING
            RETURNING config_version
            """,
            (self.tenant_id, feed_id),
        ).fetchone()
        if row is not None:
            return int(row["config_version"]), True
        version = self._lock_feed_generation(connection, feed_id)
        if version is None:  # pragma: no cover - defensive database invariant.
            raise PostgresStoreError("feed generation lock was not created")
        return version, False

    def _set_feed_generation(
        self, connection, feed_id: str, previous_version: int, next_version: int
    ):
        row = connection.execute(
            """
            UPDATE feed_generations
            SET config_version = %s, updated_at = clock_timestamp()
            WHERE tenant_id = %s AND id = %s AND config_version = %s
            RETURNING config_version
            """,
            (next_version, self.tenant_id, feed_id, previous_version),
        ).fetchone()
        if row is None:
            raise ReportConflict("feed configuration generation changed")

    def _feed_for_update(self, connection, feed_id: str):
        return connection.execute(
            """
            SELECT config, config_version FROM feeds
            WHERE tenant_id = %s AND id = %s
            FOR UPDATE
            """,
            (self.tenant_id, feed_id),
        ).fetchone()

    def _insert_feed(
        self, connection, feed_id: str, config: Mapping, version: int
    ):
        connection.execute(
            """
            INSERT INTO feeds (tenant_id, id, config, config_version)
            VALUES (%s, %s, %s::jsonb, %s)
            """,
            (self.tenant_id, feed_id, json.dumps(dict(config), sort_keys=True), version),
        )

    def _upsert_feed_with_expected_version_in_transaction(
        self, connection, config: Mapping, expected_version: int
    ) -> tuple[bool, int]:
        if not isinstance(expected_version, int) or expected_version < 0:
            raise ValueError("expected feed configuration version must be a non-negative integer")
        feed_id = str(config["id"])
        generation = self._lock_feed_generation(connection, feed_id)
        existing = self._feed_for_update(connection, feed_id)
        if expected_version == 0:
            if existing is not None:
                raise ReportConflict("feed already exists or configuration version changed")
            if generation is None:
                version, created_generation = self._lock_or_create_feed_generation(
                    connection, feed_id
                )
                if not created_generation:  # A concurrent creator committed first.
                    raise ReportConflict("feed already exists or configuration version changed")
            else:
                version = generation + 1
                self._set_feed_generation(connection, feed_id, generation, version)
            self._insert_feed(connection, feed_id, config, version)
            self._apply_feed_configuration_change(connection, feed_id, config, version)
            return True, version

        if existing is None or generation is None:
            raise ReportConflict("feed no longer exists")
        existing_version = int(existing["config_version"])
        if existing_version != expected_version or generation != expected_version:
            raise ReportConflict("feed configuration version conflict")
        if dict(existing["config"]) == dict(config):
            return False, expected_version
        version = expected_version + 1
        connection.execute(
            """
            UPDATE feeds
            SET config = %s::jsonb, config_version = %s, updated_at = clock_timestamp()
            WHERE tenant_id = %s AND id = %s AND config_version = %s
            """,
            (
                json.dumps(dict(config), sort_keys=True),
                version,
                self.tenant_id,
                feed_id,
                expected_version,
            ),
        )
        self._set_feed_generation(connection, feed_id, expected_version, version)
        self._apply_feed_configuration_change(connection, feed_id, config, version)
        return True, version

    def _apply_feed_configuration_change(
        self, connection, feed_id: str, config: Mapping, version: int
    ):
        connection.execute(
            """
            UPDATE leases
            SET state = 'revoked', expires_at = clock_timestamp()
            WHERE tenant_id = %s AND stream_id = %s
              AND state IN ('offered', 'active', 'draining')
            """,
            (self.tenant_id, feed_id),
        )
        self._suppress_disabled_alarms_for_feed(connection, feed_id, config, version)

    def _upsert_feed_in_transaction(self, connection, config: Mapping) -> tuple[bool, int]:
        feed_id = str(config["id"])
        generation, generation_created = self._lock_or_create_feed_generation(
            connection, feed_id
        )
        existing = self._feed_for_update(connection, feed_id)
        if existing is None:
            version = generation if generation_created else generation + 1
            if not generation_created:
                self._set_feed_generation(connection, feed_id, generation, version)
            self._insert_feed(connection, feed_id, config, version)
            self._apply_feed_configuration_change(connection, feed_id, config, version)
            return True, version

        existing_version = int(existing["config_version"])
        if generation_created:
            # This can only happen when repairing a manually altered legacy DB;
            # normal migrations backfill every existing feed generation.
            self._set_feed_generation(connection, feed_id, generation, existing_version)
            generation = existing_version
        if dict(existing["config"]) == dict(config):
            return False, existing_version
        version = max(generation, existing_version) + 1
        connection.execute(
            """
            UPDATE feeds
            SET config = %s::jsonb, config_version = %s, updated_at = clock_timestamp()
            WHERE tenant_id = %s AND id = %s AND config_version = %s
            """,
            (
                json.dumps(dict(config), sort_keys=True),
                version,
                self.tenant_id,
                feed_id,
                existing_version,
            ),
        )
        self._set_feed_generation(connection, feed_id, generation, version)
        self._apply_feed_configuration_change(connection, feed_id, config, version)
        return True, version

    def delete(self, feed_id: str) -> None:
        with self._pool.connection() as connection:
            with connection.transaction():
                self._delete_feed_with_expected_version_in_transaction(
                    connection, feed_id, None, missing_is_conflict=False
                )

    def delete_versioned(self, feed_id: str, expected_version: int) -> None:
        with self._pool.connection() as connection:
            with connection.transaction():
                self._delete_feed_with_expected_version_in_transaction(
                    connection, feed_id, expected_version
                )

    def _delete_feed_with_expected_version_in_transaction(
        self,
        connection,
        feed_id: str,
        expected_version: int | None,
        *,
        missing_is_conflict: bool = True,
    ) -> bool:
        if expected_version is not None and (
            not isinstance(expected_version, int) or expected_version < 1
        ):
            raise ValueError("expected feed configuration version must be positive")
        generation = self._lock_feed_generation(connection, feed_id)
        existing = self._feed_for_update(connection, feed_id)
        if existing is None:
            if missing_is_conflict:
                raise ReportConflict("feed no longer exists or configuration version changed")
            return False
        existing_version = int(existing["config_version"])
        if expected_version is not None and (
            generation != expected_version or existing_version != expected_version
        ):
            raise ReportConflict("feed no longer exists or configuration version changed")
        if generation is None:
            if expected_version is not None:
                raise ReportConflict("feed configuration generation is missing")
            generation, _ = self._lock_or_create_feed_generation(connection, feed_id)
        next_generation = max(generation, existing_version) + 1
        self._set_feed_generation(connection, feed_id, generation, next_generation)
        connection.execute(
            "DELETE FROM feeds WHERE tenant_id = %s AND id = %s AND config_version = %s",
            (self.tenant_id, feed_id, existing_version),
        )
        return True

    def delete_with_audit(
        self,
        feed_id: str,
        audit: Mapping,
        *,
        expected_version: int | None = None,
    ) -> None:
        """Commit one feed deletion and its success audit atomically."""
        with self._pool.connection() as connection:
            with connection.transaction():
                self._delete_feed_with_expected_version_in_transaction(
                    connection, feed_id, expected_version
                )
                self._append_mutation_audit(
                    connection, audit, resource_type="feed", resource_id=feed_id
                )

    def register_worker(
        self,
        worker_id: str,
        incarnation_id: uuid.UUID,
        certificate_subject: str,
        *,
        capabilities: Mapping | None = None,
        capacity: Mapping | None = None,
        software_version: str = "",
    ):
        with self._pool.connection() as connection:
            with connection.transaction():
                existing = connection.execute(
                    """
                    SELECT incarnation_id, state,
                           last_heartbeat_at > clock_timestamp()
                               - (%s * interval '1 second') AS heartbeat_fresh
                    FROM workers
                    WHERE tenant_id = %s AND worker_id = %s
                    FOR UPDATE
                    """,
                    (self.worker_freshness_seconds, self.tenant_id, worker_id),
                ).fetchone()
                if existing is not None and existing["incarnation_id"] != incarnation_id:
                    if existing["state"] == "active" and existing["heartbeat_fresh"]:
                        raise LeaseConflict(
                            "a different worker incarnation is still heartbeat-fresh"
                        )
                    connection.execute(
                        """
                        UPDATE leases
                        SET state = 'revoked', expires_at = clock_timestamp()
                        WHERE tenant_id = %s AND worker_id = %s
                          AND state IN ('offered', 'active', 'draining')
                        """,
                        (self.tenant_id, worker_id),
                    )
                capabilities_json = (
                    None
                    if capabilities is None
                    else json.dumps(dict(capabilities), sort_keys=True)
                )
                capacity_json = (
                    None if capacity is None else json.dumps(dict(capacity), sort_keys=True)
                )
                connection.execute(
                    """
                    INSERT INTO workers (
                        tenant_id, worker_id, incarnation_id, certificate_subject,
                        capabilities, capacity, software_version, state
                    ) VALUES (%s, %s, %s, %s, COALESCE(%s::jsonb, '{}'::jsonb),
                              COALESCE(%s::jsonb, '{}'::jsonb), %s, 'active')
                    ON CONFLICT (tenant_id, worker_id) DO UPDATE SET
                        incarnation_id = EXCLUDED.incarnation_id,
                        certificate_subject = EXCLUDED.certificate_subject,
                        capabilities = CASE WHEN %s::jsonb IS NULL
                                            THEN workers.capabilities
                                            ELSE EXCLUDED.capabilities END,
                        capacity = CASE WHEN %s::jsonb IS NULL
                                        THEN workers.capacity
                                        ELSE EXCLUDED.capacity END,
                        software_version = EXCLUDED.software_version,
                        state = CASE
                            WHEN workers.state = 'draining'
                             AND workers.incarnation_id = EXCLUDED.incarnation_id
                            THEN 'draining'
                            ELSE 'active'
                        END,
                        last_heartbeat_at = clock_timestamp(),
                        updated_at = clock_timestamp()
                    """,
                    (
                        self.tenant_id,
                        worker_id,
                        incarnation_id,
                        certificate_subject,
                        capabilities_json,
                        capacity_json,
                        software_version,
                        capabilities_json,
                        capacity_json,
                    ),
                )
                connection.execute(
                    """
                    UPDATE leases
                    SET expires_at = clock_timestamp()
                        + (%s * interval '1 second')
                    WHERE tenant_id = %s AND worker_id = %s
                      AND worker_incarnation_id = %s AND state = 'active'
                      AND expires_at > clock_timestamp()
                    """,
                    (
                        self.worker_freshness_seconds,
                        self.tenant_id,
                        worker_id,
                        incarnation_id,
                    ),
                )

    def active_worker_ids(self) -> list[str]:
        return [worker["id"] for worker in self.active_worker_records()]

    def active_worker_records(self, *, _connection=None) -> list[dict]:
        if _connection is None:
            with self._pool.connection() as connection:
                return self.active_worker_records(_connection=connection)
        rows = _connection.execute(
            """
            SELECT worker_id, capacity FROM workers
            WHERE tenant_id = %s AND state = 'active'
              AND last_heartbeat_at > clock_timestamp()
                  - (%s * interval '1 second')
            ORDER BY worker_id
            """,
            (self.tenant_id, self.worker_freshness_seconds),
        ).fetchall()
        return [
            {"id": row["worker_id"], "capacity": dict(row["capacity"])}
            for row in rows
        ]

    def active_lease_owners(self, *, _connection=None) -> dict[str, str]:
        if _connection is None:
            with self._pool.connection() as connection:
                return self.active_lease_owners(_connection=connection)
        rows = _connection.execute(
            """
            SELECT leases.stream_id, leases.worker_id
            FROM leases
            JOIN workers
              ON workers.tenant_id = leases.tenant_id
             AND workers.worker_id = leases.worker_id
             AND workers.incarnation_id = leases.worker_incarnation_id
            WHERE leases.tenant_id = %s
              AND leases.state IN ('offered', 'active')
              AND leases.expires_at > clock_timestamp()
              AND workers.state = 'active'
              AND workers.last_heartbeat_at > clock_timestamp()
                  - (%s * interval '1 second')
            ORDER BY leases.stream_id
            """,
            (self.tenant_id, self.worker_freshness_seconds),
        ).fetchall()
        return {row["stream_id"]: row["worker_id"] for row in rows}

    def heartbeat(self, worker_id: str, incarnation_id: uuid.UUID) -> bool:
        with self._pool.connection() as connection:
            with connection.transaction():
                row = connection.execute(
                    """
                    UPDATE workers
                    SET last_heartbeat_at = clock_timestamp(), updated_at = clock_timestamp()
                    WHERE tenant_id = %s AND worker_id = %s AND incarnation_id = %s AND state = 'active'
                    RETURNING worker_id
                    """,
                    (self.tenant_id, worker_id, incarnation_id),
                ).fetchone()
                if row is not None:
                    connection.execute(
                        """
                        UPDATE leases
                        SET expires_at = clock_timestamp()
                            + (%s * interval '1 second')
                        WHERE tenant_id = %s AND worker_id = %s
                          AND worker_incarnation_id = %s AND state = 'active'
                          AND expires_at > clock_timestamp()
                        """,
                        (
                            self.worker_freshness_seconds,
                            self.tenant_id,
                            worker_id,
                            incarnation_id,
                        ),
                    )
        return row is not None

    def drain_worker(self, worker_id: str, incarnation_id: uuid.UUID) -> list[str]:
        with self._pool.connection() as connection:
            with connection.transaction():
                worker = connection.execute(
                    """
                    UPDATE workers
                    SET state = 'draining', last_heartbeat_at = clock_timestamp(),
                        updated_at = clock_timestamp()
                    WHERE tenant_id = %s AND worker_id = %s AND incarnation_id = %s
                      AND state IN ('active', 'draining')
                    RETURNING worker_id
                    """,
                    (self.tenant_id, worker_id, incarnation_id),
                ).fetchone()
                if worker is None:
                    raise LeaseConflict("worker drain fence did not match")
                rows = connection.execute(
                    """
                    UPDATE leases
                    SET state = 'draining'
                    WHERE tenant_id = %s AND worker_id = %s
                      AND worker_incarnation_id = %s
                      AND state IN ('offered', 'active', 'draining')
                      AND expires_at > clock_timestamp()
                    RETURNING stream_id
                    """,
                    (self.tenant_id, worker_id, incarnation_id),
                ).fetchall()
        return sorted(row["stream_id"] for row in rows)

    def reconcile_lease(
        self,
        stream_id: str,
        worker_id: str,
        worker_incarnation_id: uuid.UUID,
        *,
        ttl_seconds: int,
    ) -> DurableLease:
        """Offer a new lease or renew an unchanged acknowledged lease.

        New/changed authority remains ``offered`` until the worker explicitly
        acknowledges the exact incarnation/epoch/config tuple.
        """
        return self.reconcile_leases(
            (stream_id,),
            worker_id,
            worker_incarnation_id,
            ttl_seconds=ttl_seconds,
        )[0]

    def reconcile_leases(
        self,
        stream_ids: Iterable[str],
        worker_id: str,
        worker_incarnation_id: uuid.UUID,
        *,
        ttl_seconds: int,
        _connection=None,
    ) -> list[DurableLease]:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than 0")
        requested_stream_ids = list(stream_ids)
        if len(requested_stream_ids) != len(set(requested_stream_ids)):
            raise ValueError("stream_ids must be unique")
        if not requested_stream_ids:
            return []
        if _connection is None:
            with self._pool.connection() as connection:
                with connection.transaction():
                    return self.reconcile_leases(
                        requested_stream_ids,
                        worker_id,
                        worker_incarnation_id,
                        ttl_seconds=ttl_seconds,
                        _connection=connection,
                    )
        leases = {}
        worker = _connection.execute(
            """
            SELECT incarnation_id, state,
                   last_heartbeat_at > clock_timestamp()
                       - (%s * interval '1 second') AS heartbeat_fresh
            FROM workers
            WHERE tenant_id = %s AND worker_id = %s
            FOR UPDATE
            """,
            (self.worker_freshness_seconds, self.tenant_id, worker_id),
        ).fetchone()
        if not worker or worker["state"] != "active" or worker["incarnation_id"] != worker_incarnation_id:
            raise LeaseConflict("worker incarnation is not active")
        if not worker["heartbeat_fresh"]:
            raise LeaseConflict("worker heartbeat is stale")
        for stream_id in sorted(requested_stream_ids):
            leases[stream_id] = self._reconcile_lease_in_transaction(
                _connection,
                stream_id,
                worker_id,
                worker_incarnation_id,
                ttl_seconds,
            )
        return [leases[stream_id] for stream_id in requested_stream_ids]

    def _reconcile_lease_in_transaction(
        self,
        connection,
        stream_id: str,
        worker_id: str,
        worker_incarnation_id: uuid.UUID,
        ttl_seconds: int,
    ) -> DurableLease:
        feed = connection.execute(
            "SELECT config_version FROM feeds WHERE tenant_id = %s AND id = %s FOR UPDATE",
            (self.tenant_id, stream_id),
        ).fetchone()
        if not feed:
            raise LeaseConflict("feed does not exist")
        existing = connection.execute(
            """
            SELECT *, expires_at <= clock_timestamp() AS expired
            FROM leases WHERE tenant_id = %s AND stream_id = %s FOR UPDATE
            """,
            (self.tenant_id, stream_id),
        ).fetchone()
        config_version = int(feed["config_version"])
        changed = (
            not existing
            or existing["worker_id"] != worker_id
            or existing["worker_incarnation_id"] != worker_incarnation_id
            or int(existing["config_version"]) != config_version
            or existing["state"] not in {"offered", "active"}
            or bool(existing["expired"])
        )
        epoch = 1 if not existing else int(existing["epoch"]) + (1 if changed else 0)
        lease_state = "offered" if changed else existing["state"]
        row = connection.execute(
            """
            INSERT INTO leases (
                tenant_id, stream_id, worker_id, worker_incarnation_id,
                epoch, config_version, expires_at, state
            ) VALUES (%s, %s, %s, %s, %s, %s,
                      clock_timestamp() + (%s * interval '1 second'), %s)
            ON CONFLICT (tenant_id, stream_id) DO UPDATE SET
                worker_id = EXCLUDED.worker_id,
                worker_incarnation_id = EXCLUDED.worker_incarnation_id,
                epoch = EXCLUDED.epoch,
                config_version = EXCLUDED.config_version,
                issued_at = CASE WHEN leases.epoch <> EXCLUDED.epoch THEN clock_timestamp() ELSE leases.issued_at END,
                acknowledged_at = CASE WHEN leases.epoch <> EXCLUDED.epoch THEN NULL ELSE leases.acknowledged_at END,
                last_sequence = CASE WHEN leases.epoch <> EXCLUDED.epoch THEN 0 ELSE leases.last_sequence END,
                expires_at = EXCLUDED.expires_at,
                state = EXCLUDED.state
            RETURNING tenant_id, stream_id, worker_id, worker_incarnation_id,
                      epoch, config_version, expires_at, state
            """,
            (
                self.tenant_id,
                stream_id,
                worker_id,
                worker_incarnation_id,
                epoch,
                config_version,
                ttl_seconds,
                lease_state,
            ),
        ).fetchone()
        return _lease(row)

    def acknowledge_lease(
        self,
        stream_id: str,
        worker_id: str,
        worker_incarnation_id: uuid.UUID,
        *,
        epoch: int,
        config_version: int,
        ttl_seconds: int,
    ) -> DurableLease:
        return self.acknowledge_leases(
            ((stream_id, epoch, config_version),),
            worker_id,
            worker_incarnation_id,
            ttl_seconds=ttl_seconds,
        )[0]

    def acknowledge_leases(
        self,
        leases: Iterable[tuple[str, int, int]],
        worker_id: str,
        worker_incarnation_id: uuid.UUID,
        *,
        ttl_seconds: int,
    ) -> list[DurableLease]:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than 0")
        requested = list(leases)
        stream_ids = [stream_id for stream_id, _, _ in requested]
        if len(stream_ids) != len(set(stream_ids)):
            raise ValueError("lease stream IDs must be unique")
        if not requested:
            return []
        acknowledged = {}
        with self._pool.connection() as connection:
            with connection.transaction():
                worker = connection.execute(
                    """
                    SELECT incarnation_id, state,
                           last_heartbeat_at > clock_timestamp()
                               - (%s * interval '1 second') AS heartbeat_fresh
                    FROM workers
                    WHERE tenant_id = %s AND worker_id = %s
                    FOR SHARE
                    """,
                    (self.worker_freshness_seconds, self.tenant_id, worker_id),
                ).fetchone()
                if (
                    not worker
                    or worker["state"] != "active"
                    or worker["incarnation_id"] != worker_incarnation_id
                    or not worker["heartbeat_fresh"]
                ):
                    raise LeaseConflict("worker is not fresh and active")
                for stream_id, epoch, config_version in sorted(requested):
                    acknowledged[stream_id] = self._acknowledge_lease_in_transaction(
                        connection,
                        stream_id,
                        worker_id,
                        worker_incarnation_id,
                        epoch,
                        config_version,
                        ttl_seconds,
                    )
        return [acknowledged[stream_id] for stream_id in stream_ids]

    def _acknowledge_lease_in_transaction(
        self,
        connection,
        stream_id: str,
        worker_id: str,
        worker_incarnation_id: uuid.UUID,
        epoch: int,
        config_version: int,
        ttl_seconds: int,
    ) -> DurableLease:
        feed = connection.execute(
            "SELECT config_version FROM feeds WHERE tenant_id = %s AND id = %s FOR SHARE",
            (self.tenant_id, stream_id),
        ).fetchone()
        if not feed or int(feed["config_version"]) != config_version:
            raise LeaseConflict("feed configuration changed before lease acknowledgement")
        row = connection.execute(
            """
            UPDATE leases
            SET state = 'active',
                acknowledged_at = COALESCE(acknowledged_at, clock_timestamp()),
                expires_at = clock_timestamp() + (%s * interval '1 second')
            WHERE tenant_id = %s AND stream_id = %s
              AND worker_id = %s AND worker_incarnation_id = %s
              AND epoch = %s AND config_version = %s
              AND state IN ('offered', 'active')
              AND expires_at > clock_timestamp()
            RETURNING tenant_id, stream_id, worker_id, worker_incarnation_id,
                      epoch, config_version, expires_at, state
            """,
            (
                ttl_seconds,
                self.tenant_id,
                stream_id,
                worker_id,
                worker_incarnation_id,
                epoch,
                config_version,
            ),
        ).fetchone()
        if row is None:
            raise LeaseConflict("lease acknowledgement fence did not match")
        return _lease(row)

    def revoke_unassigned_leases(
        self,
        worker_id: str,
        incarnation_id: uuid.UUID,
        assigned_stream_ids: Iterable[str],
        *,
        _connection=None,
    ) -> list[str]:
        assigned = list(assigned_stream_ids)
        if _connection is None:
            with self._pool.connection() as connection:
                with connection.transaction():
                    return self.revoke_unassigned_leases(
                        worker_id,
                        incarnation_id,
                        assigned,
                        _connection=connection,
                    )
        rows = _connection.execute(
            """
            UPDATE leases
            SET state = 'revoked', expires_at = clock_timestamp()
            WHERE tenant_id = %s AND worker_id = %s
              AND worker_incarnation_id = %s
              AND state IN ('offered', 'active', 'draining')
              AND NOT (stream_id = ANY(%s::text[]))
            RETURNING stream_id
            """,
            (self.tenant_id, worker_id, incarnation_id, assigned),
        ).fetchall()
        return sorted(row["stream_id"] for row in rows)

    def leases_for_worker(self, worker_id: str, incarnation_id: uuid.UUID) -> list[DurableLease]:
        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT tenant_id, stream_id, worker_id, worker_incarnation_id,
                       epoch, config_version, expires_at, state
                FROM leases
                WHERE tenant_id = %s AND worker_id = %s AND worker_incarnation_id = %s
                  AND state IN ('offered', 'active') AND expires_at > clock_timestamp()
                ORDER BY stream_id
                """,
                (self.tenant_id, worker_id, incarnation_id),
            ).fetchall()
        return [_lease(row) for row in rows]

    def prune_expired_alarm_events(self, *, batch_size: int = 1000) -> int:
        """Delete one bounded batch of age-expired operator history rows.

        Per-stream count retention runs at write time. This global sweep is run
        by the production pruner so stopped streams cannot retain event history
        past the configured age.
        """
        if batch_size < 1 or batch_size > 10_000:
            raise ValueError("alarm-event prune batch_size must be between 1 and 10000")
        with self._pool.connection() as connection:
            with connection.transaction():
                row = connection.execute(
                    """
                    SELECT videosim_prune_expired_alarm_events(%s, %s, %s)
                        AS deleted_count
                    """,
                    (
                        self.tenant_id,
                        self.alarm_event_retention_seconds,
                        batch_size,
                    ),
                ).fetchone()
        return int(row["deleted_count"])

    def monitor_projection_payload(
        self,
        *,
        alarm_limit: int = 100,
        event_limit: int = 200,
        pending_limit: int = 100,
    ) -> dict:
        if min(alarm_limit, event_limit, pending_limit) < 1:
            raise ValueError("monitor projection limits must be positive")
        if max(alarm_limit, event_limit, pending_limit) > 1000:
            raise ValueError("monitor projection limits must not exceed 1000")
        with self._pool.connection() as connection:
            # All response collections must describe one authority snapshot.
            # READ COMMITTED would take a new PostgreSQL snapshot for each
            # query and could expose an alarm edge without its matching row.
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            alarms = connection.execute(
                """
                SELECT a.stream_id, COALESCE(f.config->>'name', a.stream_id) AS stream_name,
                       a.monitor_id, a.active, a.severity, a.message,
                       a.raised_at, a.cleared_at, a.last_event_at, a.updated_at
                FROM current_alarms a
                JOIN feeds f ON f.tenant_id = a.tenant_id AND f.id = a.stream_id
                WHERE a.tenant_id = %s AND a.monitor_id = ANY(%s::text[])
                ORDER BY a.active DESC, a.updated_at DESC, a.stream_id, a.monitor_id
                LIMIT %s
                """,
                (self.tenant_id, sorted(SPEC_BY_ID), alarm_limit),
            ).fetchall()
            events = connection.execute(
                """
                SELECT e.event_id, e.stream_id,
                       COALESCE(f.config->>'name', e.stream_id) AS stream_name,
                       e.monitor_id, e.transition, e.payload, e.occurred_at
                FROM alarm_events e
                JOIN feeds f ON f.tenant_id = e.tenant_id AND f.id = e.stream_id
                WHERE e.tenant_id = %s AND e.monitor_id = ANY(%s::text[])
                ORDER BY e.occurred_at DESC, e.event_id DESC
                LIMIT %s
                """,
                (self.tenant_id, sorted(SPEC_BY_ID), event_limit),
            ).fetchall()
            pending = connection.execute(
                """
                SELECT p.stream_id, COALESCE(f.config->>'name', p.stream_id) AS stream_name,
                       p.monitor_id, p.severity, p.message, p.first_seen_at, p.updated_at
                FROM current_alarm_pending p
                JOIN feeds f ON f.tenant_id = p.tenant_id AND f.id = p.stream_id
                WHERE p.tenant_id = %s AND p.monitor_id = ANY(%s::text[])
                ORDER BY p.updated_at DESC, p.stream_id, p.monitor_id
                LIMIT %s
                """,
                (self.tenant_id, sorted(SPEC_BY_ID), pending_limit),
            ).fetchall()
            probe_rows = connection.execute(
                """
                SELECT wr.worker_id, c.stream_id, c.check_id, c.status,
                       c.observed_at, c.evidence
                FROM current_check_state c
                JOIN check_results r ON r.result_id = c.result_id
                JOIN worker_reports wr ON wr.report_id = r.report_id
                WHERE c.tenant_id = %s AND c.check_id LIKE 'probe.%%'
                  AND c.expires_at > transaction_timestamp()
                ORDER BY wr.worker_id, c.stream_id, c.check_id
                """,
                (self.tenant_id,),
            ).fetchall()
            workers = connection.execute(
                """
                SELECT worker_id, last_heartbeat_at, capacity
                FROM workers
                WHERE tenant_id = %s AND state = 'active'
                  AND last_heartbeat_at > transaction_timestamp()
                      - (%s * interval '1 second')
                ORDER BY worker_id
                """,
                (self.tenant_id, self.worker_freshness_seconds),
            ).fetchall()

        worker_metrics: dict[str, dict] = {}
        outcome_by_status = {
            "healthy": "success",
            "unhealthy": "issue",
            "unknown": "unknown",
            "stale": "stale",
            "error": "error",
            "timeout": "timeout",
            "skipped": "skipped",
        }
        for row in probe_rows:
            worker_id = row["worker_id"]
            evidence = dict(row["evidence"])
            metrics = worker_metrics.setdefault(
                worker_id,
                {
                    "observedAt": "",
                    "batchDurationMs": 0,
                    "streamCount": 0,
                    "checkCount": 0,
                    "outcomes": {},
                    "streams": [],
                },
            )
            outcome = outcome_by_status[row["status"]]
            item = {
                "streamId": row["stream_id"],
                "check": str(row["check_id"])[len("probe.") :],
                "outcome": outcome,
                "durationMs": evidence.get("durationMs", 0),
                "protocol": evidence.get("protocol", "unknown"),
                "source": evidence.get("source", "unknown"),
            }
            if evidence.get("message"):
                item["detail"] = str(evidence["message"])[:200]
            metrics["streams"].append(item)
            metrics["checkCount"] += 1
            metrics["outcomes"][outcome] = metrics["outcomes"].get(outcome, 0) + 1
            observed_at = _iso_datetime(row["observed_at"])
            metrics["observedAt"] = max(metrics["observedAt"], observed_at)
        for metrics in worker_metrics.values():
            metrics["streamCount"] = len({item["streamId"] for item in metrics["streams"]})

        alarm_payload = [
            {
                "id": f"{row['stream_id']}:{row['monitor_id']}",
                "streamId": row["stream_id"],
                "streamName": row["stream_name"],
                "monitorId": row["monitor_id"],
                "monitorName": SPEC_BY_ID.get(row["monitor_id"], None).name
                if row["monitor_id"] in SPEC_BY_ID
                else row["monitor_id"],
                "severity": row["severity"],
                "active": bool(row["active"]),
                "status": "active" if row["active"] else "steady",
                "raisedAt": _iso_datetime(row["raised_at"]),
                "clearedAt": _iso_datetime(row["cleared_at"]),
                "lastEventAt": _iso_datetime(row["last_event_at"]),
                "message": row["message"],
            }
            for row in alarms
        ]
        event_payload = [
            {
                "id": str(row["event_id"]),
                "time": _iso_datetime(row["occurred_at"]),
                "type": f"alarm_{row['transition']}",
                "alarmId": f"{row['stream_id']}:{row['monitor_id']}",
                "streamId": row["stream_id"],
                "streamName": row["stream_name"],
                "monitorId": row["monitor_id"],
                "monitorName": SPEC_BY_ID.get(row["monitor_id"], None).name
                if row["monitor_id"] in SPEC_BY_ID
                else row["monitor_id"],
                "severity": dict(row["payload"]).get("severity", "major"),
                "message": dict(row["payload"]).get("message", ""),
            }
            for row in reversed(events)
        ]
        pending_payload = [
            {
                "id": f"{row['stream_id']}:{row['monitor_id']}",
                "streamId": row["stream_id"],
                "streamName": row["stream_name"],
                "monitorId": row["monitor_id"],
                "monitorName": SPEC_BY_ID.get(row["monitor_id"], None).name
                if row["monitor_id"] in SPEC_BY_ID
                else row["monitor_id"],
                "severity": row["severity"],
                "message": row["message"],
                "firstSeenAt": _iso_datetime(row["first_seen_at"]),
            }
            for row in pending
        ]
        updated_candidates = [
            _iso_datetime(row["updated_at"])
            for row in alarms
        ] + [
            _iso_datetime(row["updated_at"])
            for row in pending
        ] + [
            metrics["observedAt"]
            for metrics in worker_metrics.values()
            if metrics["observedAt"]
        ]
        all_probe_streams = [
            item
            for worker in worker_metrics.values()
            for item in worker["streams"]
        ]
        all_outcomes: dict[str, int] = {}
        for item in all_probe_streams:
            outcome = item["outcome"]
            all_outcomes[outcome] = all_outcomes.get(outcome, 0) + 1
        return {
            "updatedAt": max(updated_candidates, default=""),
            "alarms": alarm_payload,
            "events": event_payload,
            "pending": pending_payload,
            "workers": [
                {
                    "id": row["worker_id"],
                    "lastSeenAt": _iso_datetime(row["last_heartbeat_at"]),
                    "pressure": dict(row["capacity"].get("pressure") or {}),
                }
                for row in workers
            ],
            "probeMetrics": {
                "streamCount": len({item["streamId"] for item in all_probe_streams}),
                "checkCount": len(all_probe_streams),
                "outcomes": all_outcomes,
                "streams": all_probe_streams,
            },
            "workerProbeMetrics": worker_metrics,
            "eventHistoryMutable": False,
            "connected": True,
        }

    def ingest_report(self, report: FencedReport) -> IngestDisposition:
        if report.tenant_id != self.tenant_id:
            raise ReportConflict("report tenant does not match repository tenant")
        if report.projection_sha256 and (
            len(report.projection_sha256) != 64
            or any(character not in "0123456789abcdef" for character in report.projection_sha256)
        ):
            raise ReportConflict("projection_sha256 must be a lowercase SHA-256 digest")
        payload = report.payload()
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        payload_hash = hashlib.sha256(encoded).hexdigest()
        with self._pool.connection() as connection:
            with connection.transaction():
                current_worker = connection.execute(
                    """
                    SELECT incarnation_id, state,
                           last_heartbeat_at > clock_timestamp()
                               - (%s * interval '1 second') AS heartbeat_fresh
                    FROM workers
                    WHERE tenant_id = %s AND worker_id = %s
                    FOR SHARE
                    """,
                    (self.worker_freshness_seconds, report.tenant_id, report.worker_id),
                ).fetchone()
                if current_worker is None:
                    raise ReportConflict("report worker is not registered")
                inserted = connection.execute(
                    """
                    INSERT INTO worker_reports (
                        report_id, tenant_id, worker_id, worker_incarnation_id, payload_sha256
                    ) VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (report_id) DO NOTHING
                    RETURNING report_id
                    """,
                    (
                        report.report_id,
                        report.tenant_id,
                        report.worker_id,
                        report.worker_incarnation_id,
                        payload_hash,
                    ),
                ).fetchone()
                if inserted is None:
                    existing = connection.execute(
                        "SELECT payload_sha256, disposition FROM worker_reports WHERE report_id = %s",
                        (report.report_id,),
                    ).fetchone()
                    if existing["payload_sha256"] != payload_hash:
                        raise ReportConflict("duplicate report ID has a different payload")
                    disposition = dict(existing["disposition"])
                    return IngestDisposition(
                        report.report_id,
                        True,
                        tuple(disposition.get("acceptedResultIds", [])),
                        tuple(disposition.get("duplicateResultIds", [])),
                        tuple(disposition.get("rejected", [])),
                    )

                worker_reason = ""
                if current_worker["state"] != "active":
                    worker_reason = "worker_not_active"
                elif current_worker["incarnation_id"] != report.worker_incarnation_id:
                    worker_reason = "worker_incarnation_mismatch"
                elif not current_worker["heartbeat_fresh"]:
                    worker_reason = "worker_heartbeat_stale"

                database_now = connection.execute(
                    "SELECT clock_timestamp() AS now"
                ).fetchone()["now"]
                accepted: list[str] = []
                duplicate_results: list[str] = []
                rejected: list[dict] = []
                max_sequences: dict[str, int] = {}
                projection_fences: dict[str, tuple[int, int, int]] = {}
                for result in sorted(report.results, key=lambda item: (item.stream_id, item.sequence, item.check_id)):
                    if worker_reason:
                        rejected.append({"resultId": str(result.result_id), "reason": worker_reason})
                        continue
                    feed = connection.execute(
                        """
                        SELECT config, config_version FROM feeds
                        WHERE tenant_id = %s AND id = %s
                        FOR SHARE
                        """,
                        (report.tenant_id, result.stream_id),
                    ).fetchone()
                    lease = connection.execute(
                        """
                        SELECT worker_id, worker_incarnation_id, epoch, config_version,
                               expires_at, expires_at <= clock_timestamp() AS expired,
                               state, last_sequence
                        FROM leases
                        WHERE tenant_id = %s AND stream_id = %s
                        FOR UPDATE
                        """,
                        (report.tenant_id, result.stream_id),
                    ).fetchone()
                    reason = _lease_rejection(
                        lease,
                        report,
                        result,
                        int(feed["config_version"]) if feed is not None else None,
                    )
                    if reason:
                        rejected.append({"resultId": str(result.result_id), "reason": reason})
                        continue
                    result_hash = hashlib.sha256(
                        json.dumps(
                            result.payload(), sort_keys=True, separators=(",", ":")
                        ).encode("utf-8")
                    ).hexdigest()
                    existing_result = connection.execute(
                        "SELECT payload_sha256 FROM check_results WHERE result_id = %s",
                        (result.result_id,),
                    ).fetchone()
                    if existing_result is not None:
                        if existing_result["payload_sha256"] == result_hash:
                            duplicate_results.append(str(result.result_id))
                        else:
                            rejected.append(
                                {
                                    "resultId": str(result.result_id),
                                    "reason": "result_id_payload_mismatch",
                                }
                            )
                        continue
                    if result.observed_at > database_now + timedelta(seconds=self.max_future_skew_seconds):
                        rejected.append({"resultId": str(result.result_id), "reason": "observed_at_in_future"})
                        continue
                    if result.observed_at < database_now - timedelta(seconds=self.result_freshness_seconds):
                        rejected.append({"resultId": str(result.result_id), "reason": "observation_too_old"})
                        continue
                    current_observation = connection.execute(
                        """
                        SELECT observed_at FROM current_check_state
                        WHERE tenant_id = %s AND stream_id = %s AND check_id = %s
                        """,
                        (report.tenant_id, result.stream_id, result.check_id),
                    ).fetchone()
                    if (
                        current_observation is not None
                        and result.observed_at < current_observation["observed_at"]
                    ):
                        rejected.append({"resultId": str(result.result_id), "reason": "observation_older_than_current"})
                        continue
                    prior_sequence = int(lease["last_sequence"])
                    if result.sequence <= prior_sequence:
                        rejected.append({"resultId": str(result.result_id), "reason": "sequence_not_newer"})
                        continue
                    result_inserted = connection.execute(
                        """
                        INSERT INTO check_results (
                            result_id, report_id, tenant_id, stream_id, check_id,
                            lease_epoch, config_version, sequence, status, observed_at,
                            evidence, payload_sha256
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                        ON CONFLICT DO NOTHING
                        RETURNING result_id
                        """,
                        (
                            result.result_id,
                            report.report_id,
                            report.tenant_id,
                            result.stream_id,
                            result.check_id,
                            result.lease_epoch,
                            result.config_version,
                            result.sequence,
                            result.status,
                            result.observed_at,
                            json.dumps(dict(result.evidence), sort_keys=True),
                            result_hash,
                        ),
                    ).fetchone()
                    if result_inserted is None:
                        existing_result = connection.execute(
                            "SELECT payload_sha256 FROM check_results WHERE result_id = %s",
                            (result.result_id,),
                        ).fetchone()
                        if (
                            existing_result is not None
                            and existing_result["payload_sha256"] == result_hash
                        ):
                            duplicate_results.append(str(result.result_id))
                        else:
                            rejected.append(
                                {
                                    "resultId": str(result.result_id),
                                    "reason": (
                                        "result_id_payload_mismatch"
                                        if existing_result is not None
                                        else "check_sequence_conflict"
                                    ),
                                }
                            )
                        continue
                    connection.execute(
                        """
                        INSERT INTO current_check_state (
                            tenant_id, stream_id, check_id, result_id, lease_epoch,
                            config_version, sequence, status, observed_at, expires_at, evidence
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                                  %s + (%s * interval '1 second'), %s::jsonb)
                        ON CONFLICT (tenant_id, stream_id, check_id) DO UPDATE SET
                            result_id = EXCLUDED.result_id,
                            lease_epoch = EXCLUDED.lease_epoch,
                            config_version = EXCLUDED.config_version,
                            sequence = EXCLUDED.sequence,
                            status = EXCLUDED.status,
                            observed_at = EXCLUDED.observed_at,
                            expires_at = EXCLUDED.expires_at,
                            evidence = EXCLUDED.evidence
                        WHERE (current_check_state.lease_epoch, current_check_state.config_version, current_check_state.sequence)
                            < (EXCLUDED.lease_epoch, EXCLUDED.config_version, EXCLUDED.sequence)
                        """,
                        (
                            report.tenant_id,
                            result.stream_id,
                            result.check_id,
                            result.result_id,
                            result.lease_epoch,
                            result.config_version,
                            result.sequence,
                            result.status,
                            result.observed_at,
                            result.observed_at,
                            self.result_freshness_seconds,
                            json.dumps(dict(result.evidence), sort_keys=True),
                        ),
                    )
                    self._apply_alarm_transition(
                        connection, report, result, dict(feed["config"])
                    )
                    accepted.append(str(result.result_id))
                    max_sequences[result.stream_id] = max(max_sequences.get(result.stream_id, prior_sequence), result.sequence)
                    projection_fences[result.stream_id] = (
                        result.lease_epoch,
                        result.config_version,
                        max(
                            projection_fences.get(
                                result.stream_id,
                                (result.lease_epoch, result.config_version, 0),
                            )[2],
                            result.sequence,
                        ),
                    )

                for stream_id, sequence in max_sequences.items():
                    connection.execute(
                        """
                        UPDATE leases SET last_sequence = GREATEST(last_sequence, %s)
                        WHERE tenant_id = %s AND stream_id = %s
                        """,
                        (sequence, report.tenant_id, stream_id),
                    )

                # Enforce age retention even during steady-state reports that
                # do not generate a new transition event.
                for stream_id in projection_fences:
                    self._prune_alarm_events(connection, report.tenant_id, stream_id)

                if report.projection_sha256:
                    for stream_id, (
                        lease_epoch,
                        config_version,
                        sequence,
                    ) in sorted(projection_fences.items()):
                        connection.execute(
                            """
                            INSERT INTO worker_projection_state (
                                tenant_id, stream_id, report_id, worker_id,
                                worker_incarnation_id, lease_epoch, config_version,
                                sequence, payload_sha256, state
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')
                            ON CONFLICT (tenant_id, stream_id) DO UPDATE SET
                                report_id = EXCLUDED.report_id,
                                worker_id = EXCLUDED.worker_id,
                                worker_incarnation_id = EXCLUDED.worker_incarnation_id,
                                lease_epoch = EXCLUDED.lease_epoch,
                                config_version = EXCLUDED.config_version,
                                sequence = EXCLUDED.sequence,
                                payload_sha256 = EXCLUDED.payload_sha256,
                                state = 'pending',
                                updated_at = clock_timestamp()
                            WHERE (
                                worker_projection_state.lease_epoch,
                                worker_projection_state.config_version,
                                worker_projection_state.sequence
                            ) < (
                                EXCLUDED.lease_epoch,
                                EXCLUDED.config_version,
                                EXCLUDED.sequence
                            )
                            """,
                            (
                                report.tenant_id,
                                stream_id,
                                report.report_id,
                                report.worker_id,
                                report.worker_incarnation_id,
                                lease_epoch,
                                config_version,
                                sequence,
                                report.projection_sha256,
                            ),
                        )

                disposition = IngestDisposition(
                    report.report_id,
                    False,
                    tuple(accepted),
                    tuple(duplicate_results),
                    tuple(rejected),
                )
                disposition_payload = disposition.payload()
                connection.execute(
                    """
                    UPDATE worker_reports
                    SET disposition = %s::jsonb, committed_at = clock_timestamp()
                    WHERE report_id = %s
                    """,
                    (json.dumps(disposition_payload, sort_keys=True), report.report_id),
                )
                if accepted:
                    event_id = uuid.uuid5(report.report_id, "results-accepted")
                    self._insert_outbox(
                        connection,
                        event_id,
                        "videosim.results.accepted.v1",
                        {
                            **disposition_payload,
                            "tenantId": report.tenant_id,
                            "workerId": report.worker_id,
                            "workerIncarnationId": str(report.worker_incarnation_id),
                        },
                    )
                return disposition

    def apply_projection_if_current(
        self,
        report_id: uuid.UUID,
        projection_sha256: str,
        fences: Mapping[str, tuple[int, int, int]],
        apply,
    ) -> set[str]:
        if not fences:
            return set()
        stream_ids = sorted(fences)
        with self._pool.connection() as connection:
            with connection.transaction():
                # Keep the live-authority lock order worker -> lease ->
                # projection. Ingestion/reconciliation takes the same order;
                # locking projection first would deadlock with a newer report
                # that already owns worker/lease locks and needs this marker.
                preliminary_rows = connection.execute(
                    """
                    SELECT stream_id, report_id, worker_id, worker_incarnation_id,
                           lease_epoch, config_version, sequence, payload_sha256, state
                    FROM worker_projection_state
                    WHERE tenant_id = %s AND stream_id = ANY(%s::text[])
                    ORDER BY stream_id
                    """,
                    (self.tenant_id, stream_ids),
                ).fetchall()
                preliminary = [
                    row
                    for row in preliminary_rows
                    if row["report_id"] == report_id
                    and row["payload_sha256"] == projection_sha256
                    and row["state"] == "pending"
                    and (
                        int(row["lease_epoch"]),
                        int(row["config_version"]),
                        int(row["sequence"]),
                    )
                    == fences[row["stream_id"]]
                ]
                if not preliminary:
                    return set()
                candidate_stream_ids = sorted(
                    row["stream_id"] for row in preliminary
                )
                worker_ids = sorted({row["worker_id"] for row in preliminary})
                workers = {
                    row["worker_id"]: row
                    for row in connection.execute(
                        """
                        SELECT worker_id, incarnation_id, state,
                               last_heartbeat_at > clock_timestamp()
                                   - (%s * interval '1 second') AS heartbeat_fresh
                        FROM workers
                        WHERE tenant_id = %s AND worker_id = ANY(%s::text[])
                        ORDER BY worker_id
                        FOR SHARE
                        """,
                        (self.worker_freshness_seconds, self.tenant_id, worker_ids),
                    ).fetchall()
                }
                leases = {
                    row["stream_id"]: row
                    for row in connection.execute(
                        """
                        SELECT stream_id, worker_id, worker_incarnation_id,
                               epoch, config_version, state,
                               expires_at > clock_timestamp() AS unexpired
                        FROM leases
                        WHERE tenant_id = %s AND stream_id = ANY(%s::text[])
                        ORDER BY stream_id
                        FOR SHARE
                        """,
                        (self.tenant_id, candidate_stream_ids),
                    ).fetchall()
                }
                rows = connection.execute(
                    """
                    SELECT stream_id, report_id, worker_id, worker_incarnation_id,
                           lease_epoch, config_version, sequence, payload_sha256, state
                    FROM worker_projection_state
                    WHERE tenant_id = %s AND stream_id = ANY(%s::text[])
                    ORDER BY stream_id
                    FOR UPDATE
                    """,
                    (self.tenant_id, candidate_stream_ids),
                ).fetchall()
                candidates = [
                    row
                    for row in rows
                    if row["report_id"] == report_id
                    and row["payload_sha256"] == projection_sha256
                    and row["state"] == "pending"
                    and (
                        int(row["lease_epoch"]),
                        int(row["config_version"]),
                        int(row["sequence"]),
                    )
                    == fences[row["stream_id"]]
                ]
                if not candidates:
                    return set()
                current = set()
                superseded = set()
                for row in candidates:
                    worker = workers.get(row["worker_id"])
                    lease = leases.get(row["stream_id"])
                    valid = (
                        worker is not None
                        and worker["state"] == "active"
                        and worker["incarnation_id"] == row["worker_incarnation_id"]
                        and bool(worker["heartbeat_fresh"])
                        and lease is not None
                        and lease["state"] == "active"
                        and bool(lease["unexpired"])
                        and lease["worker_id"] == row["worker_id"]
                        and lease["worker_incarnation_id"]
                        == row["worker_incarnation_id"]
                        and int(lease["epoch"]) == int(row["lease_epoch"])
                        and int(lease["config_version"])
                        == int(row["config_version"])
                    )
                    if valid:
                        current.add(row["stream_id"])
                    else:
                        superseded.add(row["stream_id"])
                if superseded:
                    connection.execute(
                        """
                        UPDATE worker_projection_state
                        SET state = 'superseded', updated_at = clock_timestamp()
                        WHERE tenant_id = %s AND stream_id = ANY(%s::text[])
                          AND report_id = %s AND payload_sha256 = %s
                          AND state = 'pending'
                        """,
                        (
                            self.tenant_id,
                            sorted(superseded),
                            report_id,
                            projection_sha256,
                        ),
                    )
                if not current:
                    return set()
                apply(current)
                connection.execute(
                    """
                    UPDATE worker_projection_state
                    SET state = 'applied', updated_at = clock_timestamp()
                    WHERE tenant_id = %s AND stream_id = ANY(%s::text[])
                      AND report_id = %s AND payload_sha256 = %s
                      AND state = 'pending'
                    """,
                    (
                        self.tenant_id,
                        sorted(current),
                        report_id,
                        projection_sha256,
                    ),
                )
                return current

    def _insert_outbox(
        self,
        connection,
        event_id: uuid.UUID,
        subject: str,
        payload: Mapping,
    ) -> bool:
        payload_dict = dict(payload)
        encoded = json.dumps(payload_dict, sort_keys=True, separators=(",", ":"))
        payload_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        inserted = connection.execute(
            """
            INSERT INTO outbox (event_id, subject, payload, payload_sha256)
            VALUES (%s, %s, %s::jsonb, %s)
            ON CONFLICT (event_id) DO NOTHING
            RETURNING event_id
            """,
            (event_id, subject, encoded, payload_hash),
        ).fetchone()
        if inserted is not None:
            return True
        existing = connection.execute(
            "SELECT subject, payload_sha256 FROM outbox WHERE event_id = %s",
            (event_id,),
        ).fetchone()
        if existing["subject"] != subject or existing["payload_sha256"] != payload_hash:
            raise ReportConflict("duplicate outbox event ID has a different subject or payload")
        return False

    def _append_mutation_audit(
        self,
        connection,
        audit: Mapping,
        *,
        resource_type: str,
        resource_id: str,
    ):
        required = {
            "event_id",
            "principal_kind",
            "principal_subject",
            "action",
            "outcome",
            "occurred_at",
        }
        if not isinstance(audit, Mapping) or not required.issubset(audit):
            raise ValueError("mutation audit context is incomplete")
        self._append_audit_event(
            connection,
            audit["event_id"],
            principal_kind=audit["principal_kind"],
            principal_subject=audit["principal_subject"],
            action=audit["action"],
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=audit["outcome"],
            occurred_at=audit["occurred_at"],
            payload=audit.get("payload"),
        )

    def append_audit_event(
        self,
        event_id: uuid.UUID,
        *,
        principal_kind: str,
        principal_subject: str,
        action: str,
        resource_type: str,
        resource_id: str,
        outcome: str,
        occurred_at: datetime,
        payload: Mapping | None = None,
    ):
        with self._pool.connection() as connection:
            with connection.transaction():
                self._append_audit_event(
                    connection,
                    event_id,
                    principal_kind=principal_kind,
                    principal_subject=principal_subject,
                    action=action,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    outcome=outcome,
                    occurred_at=occurred_at,
                    payload=payload,
                )

    def _append_audit_event(
        self,
        connection,
        event_id: uuid.UUID,
        *,
        principal_kind: str,
        principal_subject: str,
        action: str,
        resource_type: str,
        resource_id: str,
        outcome: str,
        occurred_at: datetime,
        payload: Mapping | None = None,
    ):
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("audit occurred_at must be timezone-aware")
        bounded = {
            "principal_kind": (principal_kind, 64),
            "principal_subject": (principal_subject, 512),
            "action": (action, 128),
            "resource_type": (resource_type, 128),
            "resource_id": (resource_id, 512),
            "outcome": (outcome, 64),
        }
        for field, (value, limit) in bounded.items():
            if not isinstance(value, str) or not value or len(value) > limit:
                raise ValueError(f"audit {field} must be a non-empty string of at most {limit} characters")
        details = dict(payload or {})
        encoded_details = json.dumps(details, sort_keys=True, separators=(",", ":"))
        if len(encoded_details.encode("utf-8")) > 16 * 1024:
            raise ValueError("audit payload must not exceed 16 KiB")
        event_payload = {
            "eventId": str(event_id),
            "tenantId": self.tenant_id,
            "principalKind": principal_kind,
            "principalSubject": principal_subject,
            "action": action,
            "resourceType": resource_type,
            "resourceId": resource_id,
            "outcome": outcome,
            "occurredAt": occurred_at.astimezone(timezone.utc).isoformat(),
            "details": details,
        }
        payload_hash = hashlib.sha256(
            json.dumps(event_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        inserted = connection.execute(
            """
            INSERT INTO audit_events (
                event_id, tenant_id, principal_kind, principal_subject,
                action, resource_type, resource_id, outcome, payload,
                payload_sha256, occurred_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            ON CONFLICT (event_id) DO NOTHING
            RETURNING event_id
            """,
            (
                event_id,
                self.tenant_id,
                principal_kind,
                principal_subject,
                action,
                resource_type,
                resource_id,
                outcome,
                encoded_details,
                payload_hash,
                occurred_at,
            ),
        ).fetchone()
        if inserted is None:
            existing = connection.execute(
                "SELECT payload_sha256 FROM audit_events WHERE event_id = %s",
                (event_id,),
            ).fetchone()
            if existing["payload_sha256"] != payload_hash:
                raise ReportConflict("duplicate audit event ID has a different payload")
        self._insert_outbox(connection, event_id, "videosim.audit.v1", event_payload)

    def enqueue_outbox(self, event_id: uuid.UUID, subject: str, payload: Mapping):
        with self._pool.connection() as connection:
            with connection.transaction():
                self._insert_outbox(connection, event_id, subject, payload)

    def claim_outbox(
        self,
        publisher_id: str,
        limit: int = 100,
        *,
        event_ids: Iterable[uuid.UUID] | None = None,
    ) -> list[OutboxRecord]:
        if limit < 1 or limit > 1000:
            raise ValueError("outbox claim limit must be between 1 and 1000")
        event_filter = list(event_ids) if event_ids is not None else None
        with self._pool.connection() as connection:
            with connection.transaction():
                rows = connection.execute(
                    """
                    WITH candidates AS (
                        SELECT id FROM outbox
                        WHERE (
                                (state = 'pending' AND next_attempt_at <= clock_timestamp())
                             OR (state = 'publishing' AND locked_at < clock_timestamp() - interval '60 seconds')
                        )
                          AND (%s::uuid[] IS NULL OR event_id = ANY(%s::uuid[]))
                        ORDER BY id
                        FOR UPDATE SKIP LOCKED
                        LIMIT %s
                    )
                    UPDATE outbox
                    SET state = 'publishing',
                        publisher_id = %s,
                        locked_at = clock_timestamp(),
                        attempts = attempts + 1
                    FROM candidates
                    WHERE outbox.id = candidates.id
                    RETURNING outbox.id, outbox.event_id, outbox.subject, outbox.payload,
                              outbox.payload_sha256, outbox.attempts
                    """,
                    (event_filter, event_filter, limit, publisher_id),
                ).fetchall()
        return [
            OutboxRecord(
                id=int(row["id"]),
                event_id=row["event_id"],
                subject=row["subject"],
                payload=dict(row["payload"]),
                payload_sha256=row["payload_sha256"],
                attempts=int(row["attempts"]),
            )
            for row in rows
        ]

    def requeue_dead_outbox(self, event_id: uuid.UUID) -> bool:
        with self._pool.connection() as connection:
            with connection.transaction():
                row = connection.execute(
                    """
                    UPDATE outbox
                    SET state = 'pending', attempts = 0, publisher_id = NULL,
                        locked_at = NULL, next_attempt_at = clock_timestamp(),
                        last_error = ''
                    WHERE event_id = %s AND state = 'dead'
                    RETURNING event_id
                    """,
                    (event_id,),
                ).fetchone()
        return row is not None

    def mark_outbox_published(self, record_id: int, publisher_id: str, broker_sequence: int):
        with self._pool.connection() as connection:
            with connection.transaction():
                row = connection.execute(
                    """
                    UPDATE outbox
                    SET state = 'published', published_at = clock_timestamp(),
                        broker_sequence = %s, last_error = ''
                    WHERE id = %s AND state = 'publishing' AND publisher_id = %s
                    RETURNING id
                    """,
                    (broker_sequence, record_id, publisher_id),
                ).fetchone()
                if row is None:
                    raise PostgresStoreError("outbox publish claim is no longer owned")

    def mark_outbox_failed(self, record_id: int, publisher_id: str, error: str):
        with self._pool.connection() as connection:
            with connection.transaction():
                row = connection.execute(
                    """
                    UPDATE outbox
                    SET state = CASE WHEN attempts >= 10 THEN 'dead' ELSE 'pending' END,
                        publisher_id = NULL,
                        locked_at = NULL,
                        next_attempt_at = clock_timestamp()
                            + (LEAST(300, power(2, attempts)::integer) * interval '1 second'),
                        last_error = %s
                    WHERE id = %s AND state = 'publishing' AND publisher_id = %s
                    RETURNING id
                    """,
                    (error[:2000], record_id, publisher_id),
                ).fetchone()
                if row is None:
                    raise PostgresStoreError("outbox failure claim is no longer owned")

    def _apply_alarm_transition(
        self,
        connection,
        report: FencedReport,
        result: CheckResult,
        feed_config: Mapping,
    ):
        # Probe metrics are operational evidence, not catalog monitor alarms.
        # Keeping them out of current_alarms prevents an aggregate
        # ``probe.validation`` row from masquerading as a user-visible monitor.
        if result.check_id.startswith("probe."):
            return
        if result.status in {"unknown", "stale", "skipped", "error", "timeout"}:
            # Inconclusive evidence updates current_check_state but can neither
            # raise nor clear authoritative alarm state or pending delay.
            return

        monitor_id = result.check_id
        spec = SPEC_BY_ID.get(monitor_id)
        message = str(result.evidence.get("message", result.status))[:2000]
        severity = (spec.severity if spec else str(result.evidence.get("severity", "major")))[:64]
        enabled, delay_seconds = self._alert_policy(feed_config, monitor_id)
        database_now = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
        existing = connection.execute(
            """
            SELECT active, last_event_at FROM current_alarms
            WHERE tenant_id = %s AND stream_id = %s AND monitor_id = %s
            FOR UPDATE
            """,
            (report.tenant_id, result.stream_id, monitor_id),
        ).fetchone()
        pending = connection.execute(
            """
            SELECT first_seen_at FROM current_alarm_pending
            WHERE tenant_id = %s AND stream_id = %s AND monitor_id = %s
            FOR UPDATE
            """,
            (report.tenant_id, result.stream_id, monitor_id),
        ).fetchone()

        if not enabled:
            self._suppress_alarm_for_result(
                connection, report, result, monitor_id, severity, message, existing
            )
            connection.execute(
                """
                DELETE FROM current_alarm_pending
                WHERE tenant_id = %s AND stream_id = %s AND monitor_id = %s
                """,
                (report.tenant_id, result.stream_id, monitor_id),
            )
            return

        if result.status == "healthy":
            connection.execute(
                """
                DELETE FROM current_alarm_pending
                WHERE tenant_id = %s AND stream_id = %s AND monitor_id = %s
                """,
                (report.tenant_id, result.stream_id, monitor_id),
            )
            if not existing or not existing["active"]:
                return
            connection.execute(
                """
                UPDATE current_alarms
                SET active = FALSE, severity = %s, message = %s,
                    source_result_id = %s, lease_epoch = %s, config_version = %s,
                    sequence = %s, cleared_at = %s, last_event_at = %s,
                    updated_at = %s
                WHERE tenant_id = %s AND stream_id = %s AND monitor_id = %s
                """,
                (
                    severity,
                    message,
                    result.result_id,
                    result.lease_epoch,
                    result.config_version,
                    result.sequence,
                    database_now,
                    database_now,
                    database_now,
                    report.tenant_id,
                    result.stream_id,
                    monitor_id,
                ),
            )
            self._append_alarm_event(
                connection, report, result, monitor_id, "cleared", severity, message
            )
            return

        # Only an explicit unhealthy observation reaches this branch.
        if existing and existing["active"]:
            repeat_due = (
                existing["last_event_at"] is None
                or database_now - existing["last_event_at"]
                >= timedelta(seconds=self.alarm_repeat_seconds)
            )
            connection.execute(
                """
                UPDATE current_alarms
                SET severity = %s, message = %s, source_result_id = %s,
                    lease_epoch = %s, config_version = %s, sequence = %s,
                    last_event_at = CASE WHEN %s THEN %s ELSE last_event_at END,
                    updated_at = %s
                WHERE tenant_id = %s AND stream_id = %s AND monitor_id = %s
                """,
                (
                    severity,
                    message,
                    result.result_id,
                    result.lease_epoch,
                    result.config_version,
                    result.sequence,
                    repeat_due,
                    database_now,
                    database_now,
                    report.tenant_id,
                    result.stream_id,
                    monitor_id,
                ),
            )
            if repeat_due:
                self._append_alarm_event(
                    connection, report, result, monitor_id, "active", severity, message
                )
            return

        if delay_seconds > 0:
            if pending is None:
                connection.execute(
                    """
                    INSERT INTO current_alarm_pending (
                        tenant_id, stream_id, monitor_id, severity, message,
                        source_result_id, lease_epoch, config_version, sequence,
                        first_seen_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        report.tenant_id,
                        result.stream_id,
                        monitor_id,
                        severity,
                        message,
                        result.result_id,
                        result.lease_epoch,
                        result.config_version,
                        result.sequence,
                        database_now,
                        database_now,
                    ),
                )
                return
            if database_now - pending["first_seen_at"] < timedelta(seconds=delay_seconds):
                connection.execute(
                    """
                    UPDATE current_alarm_pending
                    SET severity = %s, message = %s, source_result_id = %s,
                        lease_epoch = %s, config_version = %s, sequence = %s,
                        updated_at = %s
                    WHERE tenant_id = %s AND stream_id = %s AND monitor_id = %s
                    """,
                    (
                        severity,
                        message,
                        result.result_id,
                        result.lease_epoch,
                        result.config_version,
                        result.sequence,
                        database_now,
                        report.tenant_id,
                        result.stream_id,
                        monitor_id,
                    ),
                )
                return
            connection.execute(
                """
                DELETE FROM current_alarm_pending
                WHERE tenant_id = %s AND stream_id = %s AND monitor_id = %s
                """,
                (report.tenant_id, result.stream_id, monitor_id),
            )

        connection.execute(
            """
            INSERT INTO current_alarms (
                tenant_id, stream_id, monitor_id, active, severity, message,
                source_result_id, lease_epoch, config_version, sequence,
                raised_at, cleared_at, last_event_at, updated_at
            ) VALUES (%s, %s, %s, TRUE, %s, %s, %s, %s, %s, %s,
                      %s, NULL, %s, %s)
            ON CONFLICT (tenant_id, stream_id, monitor_id) DO UPDATE SET
                active = TRUE, severity = EXCLUDED.severity,
                message = EXCLUDED.message, source_result_id = EXCLUDED.source_result_id,
                lease_epoch = EXCLUDED.lease_epoch,
                config_version = EXCLUDED.config_version,
                sequence = EXCLUDED.sequence, raised_at = EXCLUDED.raised_at,
                cleared_at = NULL, last_event_at = EXCLUDED.last_event_at,
                updated_at = EXCLUDED.updated_at
            """,
            (
                report.tenant_id,
                result.stream_id,
                monitor_id,
                severity,
                message,
                result.result_id,
                result.lease_epoch,
                result.config_version,
                result.sequence,
                database_now,
                database_now,
                database_now,
            ),
        )
        self._append_alarm_event(
            connection, report, result, monitor_id, "raised", severity, message
        )

    @staticmethod
    def _alert_policy(feed_config: Mapping, monitor_id: str) -> tuple[bool, int]:
        configured = feed_config.get("alert_enabled_ids")
        if isinstance(configured, list):
            enabled = monitor_id in configured
        elif monitor_id not in SPEC_BY_ID:
            # Preserve the generic repository primitive for callers outside the
            # HTTP catalog contract; direct operator reads filter those rows.
            enabled = True
        elif feed_config.get("source") == "external":
            enabled = True
        else:
            controls = _MODE_MONITOR_CONTROLS.get(
                str(feed_config.get("mode", "normal")), _MODE_MONITOR_CONTROLS["normal"]
            )
            enabled = (
                monitor_id == "feed_reachable"
                or monitor_id.startswith("tr101_")
                or (monitor_id in {"essence_video_present", "video_frame_rate_match"} and controls["video"])
                or (monitor_id in {"essence_audio_present"} or monitor_id.startswith("loudness_")) and controls["audio"]
                or (monitor_id == "essence_captions_present" and controls["captions"])
                or (monitor_id == "black_video_detected" and controls["black"])
                or (monitor_id == "frozen_video_detected" and controls["frozen"])
            )
        try:
            delay_seconds = max(0, int(feed_config.get("alert_delay_seconds", 0)))
        except (TypeError, ValueError):
            delay_seconds = 0
        return enabled, delay_seconds

    def _suppress_disabled_alarms_for_feed(
        self,
        connection,
        stream_id: str,
        feed_config: Mapping,
        config_version: int,
    ):
        active_rows = connection.execute(
            """
            SELECT monitor_id, severity, message, source_result_id,
                   lease_epoch, sequence
            FROM current_alarms
            WHERE tenant_id = %s AND stream_id = %s AND active
            FOR UPDATE
            """,
            (self.tenant_id, stream_id),
        ).fetchall()
        pending_rows = connection.execute(
            """
            SELECT monitor_id FROM current_alarm_pending
            WHERE tenant_id = %s AND stream_id = %s
            FOR UPDATE
            """,
            (self.tenant_id, stream_id),
        ).fetchall()
        database_now = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
        for row in active_rows:
            monitor_id = row["monitor_id"]
            if self._alert_policy(feed_config, monitor_id)[0]:
                continue
            message = "Alert profile disabled this monitor"
            connection.execute(
                """
                UPDATE current_alarms
                SET active = FALSE, message = %s, config_version = %s,
                    cleared_at = %s, last_event_at = %s, updated_at = %s
                WHERE tenant_id = %s AND stream_id = %s AND monitor_id = %s
                """,
                (
                    message,
                    config_version,
                    database_now,
                    database_now,
                    database_now,
                    self.tenant_id,
                    stream_id,
                    monitor_id,
                ),
            )
            event_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"videosim:{self.tenant_id}:{stream_id}:{config_version}:{monitor_id}:suppressed",
            )
            payload = {
                "eventId": str(event_id),
                "tenantId": self.tenant_id,
                "streamId": stream_id,
                "monitorId": monitor_id,
                "transition": "suppressed",
                "resultId": str(row["source_result_id"] or ""),
                "message": message,
                "severity": row["severity"],
            }
            inserted = connection.execute(
                """
                INSERT INTO alarm_events (
                    event_id, tenant_id, stream_id, monitor_id, transition,
                    source_result_id, payload, occurred_at
                ) VALUES (%s, %s, %s, %s, 'suppressed', %s, %s::jsonb, %s)
                ON CONFLICT (event_id) DO NOTHING
                RETURNING event_id
                """,
                (
                    event_id,
                    self.tenant_id,
                    stream_id,
                    monitor_id,
                    row["source_result_id"],
                    json.dumps(payload, sort_keys=True),
                    database_now,
                ),
            ).fetchone()
            if inserted is not None:
                self._insert_outbox(
                    connection, event_id, "videosim.alarms.transition.v1", payload
                )
                self._prune_alarm_events(connection, self.tenant_id, stream_id)
        for row in pending_rows:
            if not self._alert_policy(feed_config, row["monitor_id"])[0]:
                connection.execute(
                    """
                    DELETE FROM current_alarm_pending
                    WHERE tenant_id = %s AND stream_id = %s AND monitor_id = %s
                    """,
                    (self.tenant_id, stream_id, row["monitor_id"]),
                )

    def _suppress_alarm_for_result(
        self,
        connection,
        report: FencedReport,
        result: CheckResult,
        monitor_id: str,
        severity: str,
        message: str,
        existing,
    ):
        if not existing or not existing["active"]:
            return
        database_now = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
        connection.execute(
            """
            UPDATE current_alarms
            SET active = FALSE, severity = %s, message = %s,
                source_result_id = %s, lease_epoch = %s, config_version = %s,
                sequence = %s, cleared_at = %s, last_event_at = %s,
                updated_at = %s
            WHERE tenant_id = %s AND stream_id = %s AND monitor_id = %s
            """,
            (
                severity,
                message,
                result.result_id,
                result.lease_epoch,
                result.config_version,
                result.sequence,
                database_now,
                database_now,
                database_now,
                report.tenant_id,
                result.stream_id,
                monitor_id,
            ),
        )
        self._append_alarm_event(
            connection, report, result, monitor_id, "suppressed", severity, message
        )

    def _append_alarm_event(
        self,
        connection,
        report: FencedReport,
        result: CheckResult,
        monitor_id: str,
        transition: str,
        severity: str,
        message: str,
    ):
        event_id = uuid.uuid5(result.result_id, f"alarm-{transition}")
        event_payload = {
            "eventId": str(event_id),
            "tenantId": report.tenant_id,
            "streamId": result.stream_id,
            "monitorId": monitor_id,
            "transition": transition,
            "resultId": str(result.result_id),
            "message": message,
            "severity": severity,
        }
        inserted = connection.execute(
            """
            INSERT INTO alarm_events (
                event_id, tenant_id, stream_id, monitor_id, transition,
                source_result_id, payload, occurred_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (event_id) DO NOTHING
            RETURNING event_id
            """,
            (
                event_id,
                report.tenant_id,
                result.stream_id,
                monitor_id,
                transition,
                result.result_id,
                json.dumps(event_payload, sort_keys=True),
                result.observed_at,
            ),
        ).fetchone()
        if inserted is None:
            return
        self._insert_outbox(
            connection,
            event_id,
            "videosim.alarms.transition.v1",
            event_payload,
        )
        self._prune_alarm_events(connection, report.tenant_id, result.stream_id)

    def _prune_alarm_events(self, connection, tenant_id: str, stream_id: str):
        connection.execute(
            """
            DELETE FROM alarm_events
            WHERE tenant_id = %s AND stream_id = %s
              AND created_at < clock_timestamp() - (%s * interval '1 second')
            """,
            (tenant_id, stream_id, self.alarm_event_retention_seconds),
        )
        connection.execute(
            """
            DELETE FROM alarm_events
            WHERE event_id IN (
                SELECT event_id FROM alarm_events
                WHERE tenant_id = %s AND stream_id = %s
                ORDER BY created_at DESC, event_id DESC
                OFFSET %s
            )
            """,
            (tenant_id, stream_id, self.alarm_event_history_limit),
        )


def _iso_datetime(value) -> str:
    if value is None:
        return ""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.isoformat()
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _lease(row) -> DurableLease:
    return DurableLease(
        tenant_id=row["tenant_id"],
        stream_id=row["stream_id"],
        worker_id=row["worker_id"],
        worker_incarnation_id=row["worker_incarnation_id"],
        epoch=int(row["epoch"]),
        config_version=int(row["config_version"]),
        expires_at=row["expires_at"],
        state=row["state"],
    )


def _lease_rejection(
    lease,
    report: FencedReport,
    result: CheckResult,
    current_config_version: int | None,
) -> str:
    if current_config_version is None:
        return "feed_missing"
    if not lease:
        return "lease_missing"
    if lease["state"] != "active":
        return "lease_not_active"
    if bool(lease["expired"]):
        return "lease_expired"
    if lease["worker_id"] != report.worker_id:
        return "worker_mismatch"
    if lease["worker_incarnation_id"] != report.worker_incarnation_id:
        return "worker_incarnation_mismatch"
    if int(lease["epoch"]) != result.lease_epoch:
        return "lease_epoch_mismatch"
    if int(lease["config_version"]) != result.config_version:
        return "config_version_mismatch"
    if current_config_version != result.config_version:
        return "feed_config_version_mismatch"
    return ""
