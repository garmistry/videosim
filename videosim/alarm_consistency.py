from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .postgres_store import DEFAULT_TENANT_ID, PostgresControlPlaneStore
from .scale_evidence import WORKLOAD_SCHEMA


REPORT_SCHEMA = "videosim.alarm-consistency/v1"
_ALARM_SUBJECT = "videosim.alarms.transition.v1"


@dataclass(frozen=True)
class AlarmConsistencyReport:
    captured_at: str
    tenant_id: str
    workload_content_sha256: str
    metrics: dict
    checks: tuple[dict, ...]
    errors: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.errors

    def payload(self) -> dict:
        return {
            "schemaVersion": REPORT_SCHEMA,
            "passed": self.passed,
            "capturedAt": self.captured_at,
            "tenantId": self.tenant_id,
            "workloadContentSha256": self.workload_content_sha256,
            "metrics": self.metrics,
            "checks": list(self.checks),
            "errors": list(self.errors),
        }

    def to_json(self) -> str:
        return json.dumps(self.payload(), indent=2, sort_keys=True)


def capture_alarm_consistency(
    store: PostgresControlPlaneStore,
    workload: dict,
    *,
    _connection=None,
) -> AlarmConsistencyReport:
    expected_streams = _expected_streams(workload)
    if _connection is not None:
        return _capture(
            _connection,
            store.tenant_id,
            expected_streams,
            _sha256(workload),
        )
    with store._pool.connection() as connection:
        with connection.transaction():
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            )
            return _capture(
                connection,
                store.tenant_id,
                expected_streams,
                _sha256(workload),
            )


def run_alarm_consistency(
    database_url: str,
    workload_path: str,
    *,
    output_path: str = "",
    tenant_id: str = DEFAULT_TENANT_ID,
) -> AlarmConsistencyReport:
    workload = _read_workload(Path(workload_path))
    store = PostgresControlPlaneStore(database_url, tenant_id=tenant_id)
    try:
        report = capture_alarm_consistency(store, workload)
    finally:
        store.close()
    if output_path:
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp")
        temporary.write_text(report.to_json() + "\n", encoding="utf-8")
        os.replace(temporary, destination)
    return report


def human_summary(report: AlarmConsistencyReport) -> str:
    metrics = report.metrics
    summary = (
        f"Alarm consistency {'passed' if report.passed else 'failed'}: "
        f"desired={metrics.get('desiredStreams', 0)} "
        f"observed={metrics.get('observedDesiredStreams', 0)} "
        f"alarms={metrics.get('currentAlarms', 0)} "
        f"events={metrics.get('alarmEvents', 0)}"
    )
    return summary if not report.errors else summary + "\n" + "\n".join(
        f"- {error}" for error in report.errors
    )


def _capture(
    connection,
    tenant_id: str,
    expected_streams: int,
    workload_content_sha256: str,
) -> AlarmConsistencyReport:
    captured_at = connection.execute(
        "SELECT transaction_timestamp() AS captured_at"
    ).fetchone()["captured_at"]
    metrics = dict(
        connection.execute(_METRICS_SQL, {"tenant": tenant_id}).fetchone()
    )
    metrics = {key: int(value) for key, value in metrics.items()}
    metrics["expectedDesiredStreams"] = expected_streams
    checks = tuple(
        {
            "name": row["check_name"],
            "passed": int(row["violation_count"]) == 0,
            "violations": int(row["violation_count"]),
            "samples": list(row["samples"]),
        }
        for row in connection.execute(
            _CONSISTENCY_SQL,
            {"tenant": tenant_id, "alarm_subject": _ALARM_SUBJECT},
        ).fetchall()
    )
    errors = []
    if metrics["desiredStreams"] != expected_streams:
        errors.append(
            f"desired stream count is {metrics['desiredStreams']}; expected {expected_streams}"
        )
    if metrics["observedDesiredStreams"] != expected_streams:
        errors.append(
            "desired streams with current check state are "
            f"{metrics['observedDesiredStreams']}; expected {expected_streams}"
        )
    if metrics["alarmEvents"] == 0:
        errors.append("no retained alarm transitions; consistency is unexercised")
    for check in checks:
        if not check["passed"]:
            sample = ", ".join(check["samples"])
            errors.append(
                f"{check['name']} has {check['violations']} violation(s)"
                + (f": {sample}" if sample else "")
            )
    return AlarmConsistencyReport(
        captured_at=_iso(captured_at),
        tenant_id=tenant_id,
        workload_content_sha256=workload_content_sha256,
        metrics=metrics,
        checks=checks,
        errors=tuple(errors),
    )


def _read_workload(path: Path) -> dict:
    try:
        workload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"workload could not be read as JSON: {exc}") from exc
    _expected_streams(workload)
    return workload


def _expected_streams(workload: object) -> int:
    if not isinstance(workload, dict) or workload.get("schemaVersion") != WORKLOAD_SCHEMA:
        raise ValueError(f"workload.schemaVersion must be {WORKLOAD_SCHEMA}")
    expected = workload.get("loadStreams")
    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
        raise ValueError("workload.loadStreams must be a positive integer")
    return expected


def _sha256(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


_METRICS_SQL = """
WITH desired AS (
    SELECT id
    FROM feeds
    WHERE tenant_id = %(tenant)s
      AND (
          config ->> 'source' = 'external'
          OR (config ->> 'source' = 'generated'
              AND config ->> 'desired_state' = 'running')
      )
)
SELECT
    (SELECT count(*) FROM desired) AS "desiredStreams",
    (SELECT count(DISTINCT c.stream_id)
     FROM current_check_state c JOIN desired d ON d.id = c.stream_id
     WHERE c.tenant_id = %(tenant)s) AS "observedDesiredStreams",
    (SELECT count(*) FROM check_results WHERE tenant_id = %(tenant)s)
        AS "checkResults",
    (SELECT count(*) FROM current_check_state WHERE tenant_id = %(tenant)s)
        AS "currentChecks",
    (SELECT count(*) FROM current_alarm_pending WHERE tenant_id = %(tenant)s)
        AS "pendingAlarms",
    (SELECT count(*) FROM current_alarms WHERE tenant_id = %(tenant)s)
        AS "currentAlarms",
    (SELECT count(*) FROM alarm_events WHERE tenant_id = %(tenant)s)
        AS "alarmEvents",
    (SELECT count(*) FROM alarm_events e JOIN outbox o ON o.event_id = e.event_id
     WHERE e.tenant_id = %(tenant)s) AS "alarmOutboxEvents"
"""


_CONSISTENCY_SQL = """
WITH latest_results AS (
    SELECT DISTINCT ON (tenant_id, stream_id, check_id)
           result_id, tenant_id, stream_id, check_id, lease_epoch,
           config_version, sequence, status, observed_at, evidence
    FROM check_results
    WHERE tenant_id = %(tenant)s
    ORDER BY tenant_id, stream_id, check_id, lease_epoch DESC,
             config_version DESC, sequence DESC, observed_at DESC, result_id DESC
), latest_events AS (
    SELECT DISTINCT ON (tenant_id, stream_id, monitor_id)
           event_id, tenant_id, stream_id, monitor_id, transition,
           source_result_id
    FROM alarm_events
    WHERE tenant_id = %(tenant)s
    ORDER BY tenant_id, stream_id, monitor_id, created_at DESC, event_id DESC
), current_state_source AS (
    SELECT c.stream_id || '/' || c.check_id AS key
    FROM current_check_state c
    LEFT JOIN check_results r ON r.result_id = c.result_id
    WHERE c.tenant_id = %(tenant)s
      AND (
          r.result_id IS NULL
          OR (r.tenant_id, r.stream_id, r.check_id, r.lease_epoch,
              r.config_version, r.sequence, r.status, r.observed_at)
             IS DISTINCT FROM
             (c.tenant_id, c.stream_id, c.check_id, c.lease_epoch,
              c.config_version, c.sequence, c.status, c.observed_at)
          OR r.evidence IS DISTINCT FROM c.evidence
      )
), latest_result_projection AS (
    SELECT r.stream_id || '/' || r.check_id AS key
    FROM latest_results r
    LEFT JOIN current_check_state c
      ON c.tenant_id = r.tenant_id AND c.stream_id = r.stream_id
     AND c.check_id = r.check_id
    WHERE c.result_id IS DISTINCT FROM r.result_id
), pending_alarm_source AS (
    SELECT p.stream_id || '/' || p.monitor_id AS key
    FROM current_alarm_pending p
    LEFT JOIN check_results r ON r.result_id = p.source_result_id
    LEFT JOIN current_alarms a
      ON a.tenant_id = p.tenant_id AND a.stream_id = p.stream_id
     AND a.monitor_id = p.monitor_id AND a.active
    LEFT JOIN current_check_state c
      ON c.tenant_id = p.tenant_id AND c.stream_id = p.stream_id
     AND c.check_id = p.monitor_id
    WHERE p.tenant_id = %(tenant)s
      AND (
          r.result_id IS NULL OR r.tenant_id IS DISTINCT FROM p.tenant_id
          OR r.stream_id IS DISTINCT FROM p.stream_id
          OR r.check_id IS DISTINCT FROM p.monitor_id
          OR r.status IS DISTINCT FROM 'unhealthy'
          OR (r.lease_epoch, r.config_version, r.sequence) IS DISTINCT FROM
             (p.lease_epoch, p.config_version, p.sequence)
          OR p.first_seen_at > p.updated_at OR a.monitor_id IS NOT NULL
          OR p.monitor_id LIKE 'probe.%%'
          OR (c.status IN ('healthy', 'unhealthy')
              AND c.result_id IS DISTINCT FROM p.source_result_id)
      )
), current_alarm_source AS (
    SELECT a.stream_id || '/' || a.monitor_id AS key
    FROM current_alarms a
    LEFT JOIN check_results r ON r.result_id = a.source_result_id
    LEFT JOIN current_check_state c
      ON c.tenant_id = a.tenant_id AND c.stream_id = a.stream_id
     AND c.check_id = a.monitor_id
    WHERE a.tenant_id = %(tenant)s
      AND (
          r.result_id IS NULL OR r.tenant_id IS DISTINCT FROM a.tenant_id
          OR r.stream_id IS DISTINCT FROM a.stream_id
          OR r.check_id IS DISTINCT FROM a.monitor_id
          OR a.monitor_id LIKE 'probe.%%'
          OR a.raised_at IS NULL
          OR (a.active AND (
              r.status IS DISTINCT FROM 'unhealthy'
              OR (r.lease_epoch, r.config_version, r.sequence) IS DISTINCT FROM
                 (a.lease_epoch, a.config_version, a.sequence)
              OR a.cleared_at IS NOT NULL
              OR (c.status IN ('healthy', 'unhealthy')
                  AND c.result_id IS DISTINCT FROM a.source_result_id)
          ))
          OR (NOT a.active AND a.cleared_at IS NULL)
      )
), alarm_event_projection AS (
    SELECT COALESCE(a.stream_id, e.stream_id) || '/' ||
           COALESCE(a.monitor_id, e.monitor_id) AS key
    FROM current_alarms a
    FULL OUTER JOIN latest_events e
      ON e.tenant_id = a.tenant_id AND e.stream_id = a.stream_id
     AND e.monitor_id = a.monitor_id
    WHERE COALESCE(a.tenant_id, e.tenant_id) = %(tenant)s
      AND (
          a.monitor_id IS NULL OR e.event_id IS NULL
          OR (a.active AND e.transition NOT IN ('raised', 'active', 'acknowledged'))
          OR (NOT a.active AND e.transition NOT IN ('cleared', 'suppressed'))
          OR (NOT a.active AND a.source_result_id IS DISTINCT FROM e.source_result_id)
      )
), alarm_event_source AS (
    SELECT e.event_id::text AS key
    FROM alarm_events e
    LEFT JOIN check_results r ON r.result_id = e.source_result_id
    WHERE e.tenant_id = %(tenant)s
      AND (
          r.result_id IS NULL OR r.tenant_id IS DISTINCT FROM e.tenant_id
          OR r.stream_id IS DISTINCT FROM e.stream_id
          OR r.check_id IS DISTINCT FROM e.monitor_id
          OR e.monitor_id LIKE 'probe.%%'
          OR (e.transition IN ('raised', 'active', 'acknowledged')
              AND r.status IS DISTINCT FROM 'unhealthy')
          OR (e.transition = 'cleared' AND r.status IS DISTINCT FROM 'healthy')
          OR (e.transition = 'suppressed'
              AND r.status NOT IN ('healthy', 'unhealthy'))
      )
), alarm_event_payload AS (
    SELECT e.event_id::text AS key
    FROM alarm_events e
    WHERE e.tenant_id = %(tenant)s
      AND (
          e.payload ->> 'eventId' IS DISTINCT FROM e.event_id::text
          OR e.payload ->> 'tenantId' IS DISTINCT FROM e.tenant_id
          OR e.payload ->> 'streamId' IS DISTINCT FROM e.stream_id
          OR e.payload ->> 'monitorId' IS DISTINCT FROM e.monitor_id
          OR e.payload ->> 'transition' IS DISTINCT FROM e.transition
          OR e.payload ->> 'resultId' IS DISTINCT FROM e.source_result_id::text
          OR jsonb_typeof(e.payload -> 'message') IS DISTINCT FROM 'string'
          OR COALESCE(e.payload ->> 'message', '') = ''
          OR jsonb_typeof(e.payload -> 'severity') IS DISTINCT FROM 'string'
          OR COALESCE(e.payload ->> 'severity', '') = ''
      )
), alarm_event_outbox AS (
    SELECT e.event_id::text AS key
    FROM alarm_events e
    LEFT JOIN outbox o ON o.event_id = e.event_id
    WHERE e.tenant_id = %(tenant)s
      AND (
          o.event_id IS NULL OR o.subject IS DISTINCT FROM %(alarm_subject)s
          OR o.payload IS DISTINCT FROM e.payload
      )
), violations AS (
    SELECT 'current-state-source' AS check_name, key FROM current_state_source
    UNION ALL SELECT 'latest-result-projection', key FROM latest_result_projection
    UNION ALL SELECT 'pending-alarm-source', key FROM pending_alarm_source
    UNION ALL SELECT 'current-alarm-source', key FROM current_alarm_source
    UNION ALL SELECT 'alarm-event-projection', key FROM alarm_event_projection
    UNION ALL SELECT 'alarm-event-source', key FROM alarm_event_source
    UNION ALL SELECT 'alarm-event-payload', key FROM alarm_event_payload
    UNION ALL SELECT 'alarm-event-outbox', key FROM alarm_event_outbox
), check_names(check_name) AS (
    VALUES ('current-state-source'), ('latest-result-projection'),
           ('pending-alarm-source'), ('current-alarm-source'),
           ('alarm-event-projection'), ('alarm-event-source'),
           ('alarm-event-payload'), ('alarm-event-outbox')
)
SELECT n.check_name, count(v.key) AS violation_count,
       COALESCE(
           (array_agg(v.key ORDER BY v.key) FILTER (WHERE v.key IS NOT NULL))[1:5],
           ARRAY[]::text[]
       ) AS samples
FROM check_names n
LEFT JOIN violations v ON v.check_name = n.check_name
GROUP BY n.check_name
ORDER BY n.check_name
"""
