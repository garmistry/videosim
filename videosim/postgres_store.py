from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping


DEFAULT_TENANT_ID = "default"


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

    def payload(self) -> dict:
        return {
            "reportId": str(self.report_id),
            "tenantId": self.tenant_id,
            "workerId": self.worker_id,
            "workerIncarnationId": str(self.worker_incarnation_id),
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
    ):
        if not database_url:
            raise ValueError("database_url is required")
        if min_pool_size < 0 or max_pool_size < 1 or min_pool_size > max_pool_size:
            raise ValueError("invalid PostgreSQL pool size")
        if min(worker_freshness_seconds, max_future_skew_seconds, result_freshness_seconds) <= 0:
            raise ValueError("freshness and clock-skew limits must be greater than 0")
        ConnectionPool, dict_row = _postgres_modules()
        self.database_url = database_url
        self.tenant_id = tenant_id
        self.worker_freshness_seconds = worker_freshness_seconds
        self.max_future_skew_seconds = max_future_skew_seconds
        self.result_freshness_seconds = result_freshness_seconds
        self._pool = ConnectionPool(
            conninfo=database_url,
            min_size=min_pool_size,
            max_size=max_pool_size,
            kwargs={"row_factory": dict_row},
            open=True,
        )

    def close(self):
        self._pool.close()

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
                row = connection.execute(
                    """
                    UPDATE feeds
                    SET config = %s::jsonb,
                        config_version = config_version + 1,
                        updated_at = clock_timestamp()
                    WHERE tenant_id = %s AND id = %s AND config_version = %s
                      AND config IS DISTINCT FROM %s::jsonb
                    RETURNING config_version
                    """,
                    (
                        json.dumps(config, sort_keys=True),
                        self.tenant_id,
                        str(feed["id"]),
                        expected_version,
                        json.dumps(config, sort_keys=True),
                    ),
                ).fetchone()
                if row is not None:
                    connection.execute(
                        """
                        UPDATE leases
                        SET state = 'revoked', expires_at = clock_timestamp()
                        WHERE tenant_id = %s AND stream_id = %s
                          AND state IN ('offered', 'active', 'draining')
                        """,
                        (self.tenant_id, str(feed["id"])),
                    )
                    return int(row["config_version"])
                existing = connection.execute(
                    "SELECT config, config_version FROM feeds WHERE tenant_id = %s AND id = %s",
                    (self.tenant_id, str(feed["id"])),
                ).fetchone()
                if (
                    existing is not None
                    and int(existing["config_version"]) == expected_version
                    and dict(existing["config"]) == config
                ):
                    return expected_version
                raise ReportConflict("feed configuration version conflict")

    def import_feed_if_changed(self, feed: Mapping) -> tuple[bool, int]:
        config = dict(feed)
        config.pop("config_version", None)
        with self._pool.connection() as connection:
            with connection.transaction():
                row = connection.execute(
                    """
                    INSERT INTO feeds (tenant_id, id, config)
                    VALUES (%s, %s, %s::jsonb)
                    ON CONFLICT (tenant_id, id) DO UPDATE SET
                        config = EXCLUDED.config,
                        config_version = feeds.config_version + 1,
                        updated_at = clock_timestamp()
                    WHERE feeds.config IS DISTINCT FROM EXCLUDED.config
                    RETURNING config_version
                    """,
                    (self.tenant_id, str(feed["id"]), json.dumps(config, sort_keys=True)),
                ).fetchone()
                if row is not None:
                    connection.execute(
                        """
                        UPDATE leases
                        SET state = 'revoked', expires_at = clock_timestamp()
                        WHERE tenant_id = %s AND stream_id = %s
                          AND state IN ('offered', 'active', 'draining')
                        """,
                        (self.tenant_id, str(feed["id"])),
                    )
                    return True, int(row["config_version"])
                existing = connection.execute(
                    "SELECT config_version FROM feeds WHERE tenant_id = %s AND id = %s",
                    (self.tenant_id, str(feed["id"])),
                ).fetchone()
                return False, int(existing["config_version"])

    def delete(self, feed_id: str) -> None:
        with self._pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    "DELETE FROM feeds WHERE tenant_id = %s AND id = %s",
                    (self.tenant_id, feed_id),
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
                    SELECT incarnation_id FROM workers
                    WHERE tenant_id = %s AND worker_id = %s
                    FOR UPDATE
                    """,
                    (self.tenant_id, worker_id),
                ).fetchone()
                if existing is not None and existing["incarnation_id"] != incarnation_id:
                    connection.execute(
                        """
                        UPDATE leases
                        SET state = 'revoked', expires_at = clock_timestamp()
                        WHERE tenant_id = %s AND worker_id = %s
                          AND state IN ('offered', 'active', 'draining')
                        """,
                        (self.tenant_id, worker_id),
                    )
                connection.execute(
                    """
                    INSERT INTO workers (
                        tenant_id, worker_id, incarnation_id, certificate_subject,
                        capabilities, capacity, software_version, state
                    ) VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, 'active')
                    ON CONFLICT (tenant_id, worker_id) DO UPDATE SET
                        incarnation_id = EXCLUDED.incarnation_id,
                        certificate_subject = EXCLUDED.certificate_subject,
                        capabilities = EXCLUDED.capabilities,
                        capacity = EXCLUDED.capacity,
                        software_version = EXCLUDED.software_version,
                        state = 'active',
                        last_heartbeat_at = clock_timestamp(),
                        updated_at = clock_timestamp()
                    """,
                    (
                        self.tenant_id,
                        worker_id,
                        incarnation_id,
                        certificate_subject,
                        json.dumps(dict(capabilities or {}), sort_keys=True),
                        json.dumps(dict(capacity or {}), sort_keys=True),
                        software_version,
                    ),
                )

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
        return row is not None

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
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than 0")
        with self._pool.connection() as connection:
            with connection.transaction():
                worker = connection.execute(
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
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than 0")
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

    def ingest_report(self, report: FencedReport) -> IngestDisposition:
        if report.tenant_id != self.tenant_id:
            raise ReportConflict("report tenant does not match repository tenant")
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
                for result in sorted(report.results, key=lambda item: (item.stream_id, item.sequence, item.check_id)):
                    if worker_reason:
                        rejected.append({"resultId": str(result.result_id), "reason": worker_reason})
                        continue
                    feed = connection.execute(
                        """
                        SELECT config_version FROM feeds
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
                    self._apply_alarm_transition(connection, report, result)
                    accepted.append(str(result.result_id))
                    max_sequences[result.stream_id] = max(max_sequences.get(result.stream_id, prior_sequence), result.sequence)

                for stream_id, sequence in max_sequences.items():
                    connection.execute(
                        """
                        UPDATE leases SET last_sequence = GREATEST(last_sequence, %s)
                        WHERE tenant_id = %s AND stream_id = %s
                        """,
                        (sequence, report.tenant_id, stream_id),
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
                        disposition_payload,
                    )
                return disposition

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
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("audit occurred_at must be timezone-aware")
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
            "details": dict(payload or {}),
        }
        payload_hash = hashlib.sha256(
            json.dumps(event_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        with self._pool.connection() as connection:
            with connection.transaction():
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
                        json.dumps(dict(payload or {}), sort_keys=True),
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

    def _apply_alarm_transition(self, connection, report: FencedReport, result: CheckResult):
        if result.status in {"unknown", "stale", "skipped", "error", "timeout"}:
            # Inconclusive evidence updates current_check_state but can neither
            # raise nor clear authoritative alarm state.
            return
        active = result.status == "unhealthy"
        existing = connection.execute(
            """
            SELECT active FROM current_alarms
            WHERE tenant_id = %s AND stream_id = %s AND monitor_id = %s
            FOR UPDATE
            """,
            (report.tenant_id, result.stream_id, result.check_id),
        ).fetchone()
        previous_active = bool(existing["active"]) if existing else False
        message = str(result.evidence.get("message", result.status))[:2000]
        severity = str(result.evidence.get("severity", "major"))[:64]
        connection.execute(
            """
            INSERT INTO current_alarms (
                tenant_id, stream_id, monitor_id, active, severity, message,
                source_result_id, lease_epoch, config_version, sequence,
                raised_at, cleared_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      CASE WHEN %s THEN clock_timestamp() ELSE NULL END,
                      CASE WHEN %s THEN NULL ELSE clock_timestamp() END)
            ON CONFLICT (tenant_id, stream_id, monitor_id) DO UPDATE SET
                active = EXCLUDED.active,
                severity = EXCLUDED.severity,
                message = EXCLUDED.message,
                source_result_id = EXCLUDED.source_result_id,
                lease_epoch = EXCLUDED.lease_epoch,
                config_version = EXCLUDED.config_version,
                sequence = EXCLUDED.sequence,
                raised_at = CASE WHEN NOT current_alarms.active AND EXCLUDED.active THEN clock_timestamp() ELSE current_alarms.raised_at END,
                cleared_at = CASE WHEN current_alarms.active AND NOT EXCLUDED.active THEN clock_timestamp() ELSE current_alarms.cleared_at END,
                updated_at = clock_timestamp()
            """,
            (
                report.tenant_id,
                result.stream_id,
                result.check_id,
                active,
                severity,
                message,
                result.result_id,
                result.lease_epoch,
                result.config_version,
                result.sequence,
                active,
                active,
            ),
        )
        if active == previous_active:
            return
        transition = "raised" if active else "cleared"
        event_id = uuid.uuid5(result.result_id, f"alarm-{transition}")
        event_payload = {
            "eventId": str(event_id),
            "tenantId": report.tenant_id,
            "streamId": result.stream_id,
            "monitorId": result.check_id,
            "transition": transition,
            "resultId": str(result.result_id),
            "message": message,
            "severity": severity,
        }
        connection.execute(
            """
            INSERT INTO alarm_events (
                event_id, tenant_id, stream_id, monitor_id, transition,
                source_result_id, payload, occurred_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (event_id) DO NOTHING
            """,
            (
                event_id,
                report.tenant_id,
                result.stream_id,
                result.check_id,
                transition,
                result.result_id,
                json.dumps(event_payload, sort_keys=True),
                result.observed_at,
            ),
        )
        self._insert_outbox(
            connection,
            event_id,
            "videosim.alarms.transition.v1",
            event_payload,
        )


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
