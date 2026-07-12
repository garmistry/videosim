#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys


SEMANTIC_QUERIES = {
    "schema_migrations": "SELECT version, name, checksum FROM schema_migrations ORDER BY version",
    "feeds": "SELECT tenant_id, id, config, config_version FROM feeds ORDER BY tenant_id, id",
    "workers": """
        SELECT tenant_id, worker_id, incarnation_id, certificate_subject,
               capabilities, capacity, software_version, state
        FROM workers ORDER BY tenant_id, worker_id
    """,
    # Restore intentionally expires leases and changes expiry timestamps. Compare
    # ownership/fencing semantics while allowing those safety mutations.
    "leases": """
        SELECT tenant_id, stream_id, worker_id, worker_incarnation_id,
               epoch, config_version, last_sequence
        FROM leases ORDER BY tenant_id, stream_id
    """,
    "worker_reports": """
        SELECT report_id, tenant_id, worker_id, worker_incarnation_id,
               payload_sha256, disposition
        FROM worker_reports ORDER BY report_id
    """,
    "check_results": """
        SELECT result_id, report_id, tenant_id, stream_id, check_id,
               lease_epoch, config_version, sequence, status, observed_at,
               evidence, payload_sha256
        FROM check_results ORDER BY result_id
    """,
    "current_check_state": """
        SELECT tenant_id, stream_id, check_id, result_id, lease_epoch,
               config_version, sequence, status, observed_at, evidence
        FROM current_check_state ORDER BY tenant_id, stream_id, check_id
    """,
    "worker_projection_state": """
        SELECT tenant_id, stream_id, report_id, worker_id,
               worker_incarnation_id, lease_epoch, config_version, sequence,
               payload_sha256, state
        FROM worker_projection_state ORDER BY tenant_id, stream_id
    """,
    "current_alarms": """
        SELECT tenant_id, stream_id, monitor_id, active, severity, message,
               source_result_id, lease_epoch, config_version, sequence,
               raised_at, cleared_at
        FROM current_alarms ORDER BY tenant_id, stream_id, monitor_id
    """,
    "alarm_events": """
        SELECT event_id, tenant_id, stream_id, monitor_id, transition,
               source_result_id, payload, occurred_at
        FROM alarm_events ORDER BY event_id
    """,
    "audit_events": """
        SELECT event_id, tenant_id, principal_kind, principal_subject, action,
               resource_type, resource_id, outcome, payload, payload_sha256,
               occurred_at
        FROM audit_events ORDER BY event_id
    """,
    # Delivery state can intentionally differ when a restore targets an empty
    # broker. Immutable event identity/content must still match.
    "outbox": """
        SELECT event_id, subject, payload, payload_sha256
        FROM outbox ORDER BY event_id
    """,
    "consumer_inbox": """
        SELECT consumer_name, event_id, payload_sha256
        FROM consumer_inbox ORDER BY consumer_name, event_id
    """,
}


def parse_args():
    parser = argparse.ArgumentParser(description="Compare source and restored VideoSim PostgreSQL semantics")
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--restored-url", required=True)
    return parser.parse_args()


def digest_query(connection, query: str, cursor_name: str) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    with connection.cursor(name=cursor_name) as cursor:
        cursor.execute(query)
        while True:
            rows = cursor.fetchmany(1000)
            if not rows:
                break
            for row in rows:
                encoded = json.dumps(
                    dict(row),
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
                count += 1
    return count, digest.hexdigest()


def snapshot(database_url: str) -> dict:
    import psycopg
    from psycopg.rows import dict_row

    table_digests = {}
    counts = {}
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        connection.execute(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
        )
        for index, (table, query) in enumerate(SEMANTIC_QUERIES.items()):
            count, digest = digest_query(connection, query, f"restore_verify_{index}")
            counts[table] = count
            table_digests[table] = digest
        generation = connection.execute(
            "SELECT generation FROM control_plane_state WHERE singleton = TRUE"
        ).fetchone()["generation"]
    semantic = json.dumps(table_digests, sort_keys=True, separators=(",", ":"))
    return {
        "semanticSha256": hashlib.sha256(semantic.encode("utf-8")).hexdigest(),
        "tableSha256": table_digests,
        "counts": counts,
        "generation": int(generation),
    }


def main() -> int:
    args = parse_args()
    source = snapshot(args.source_url)
    restored = snapshot(args.restored_url)
    passed = source["semanticSha256"] == restored["semanticSha256"]
    payload = {"passed": passed, "source": source, "restored": restored}
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
