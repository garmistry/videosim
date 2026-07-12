#!/usr/bin/env bash
set -euo pipefail

backup="${1:?usage: scripts/postgres-restore.sh BACKUP.dump}"
source_url="${VIDEOSIM_DATABASE_URL:?VIDEOSIM_DATABASE_URL is required for source/target safety comparison}"
target_url="${VIDEOSIM_RESTORE_DATABASE_URL:?VIDEOSIM_RESTORE_DATABASE_URL is required}"
expected_target="${VIDEOSIM_RESTORE_EXPECTED_TARGET:?set to the exact host:port/database target identity}"
confirmation="${VIDEOSIM_RESTORE_CONFIRM:?set to DESTROY_AND_RESTORE followed by the exact target identity}"
broker_mode="${VIDEOSIM_RESTORE_BROKER_MODE:?set to empty or retained for the promoted JetStream state}"

case "$broker_mode" in
  empty|retained) ;;
  *) echo "VIDEOSIM_RESTORE_BROKER_MODE must be empty or retained" >&2; exit 1 ;;
esac

if [ ! -f "$backup" ]; then
  echo "backup does not exist: $backup" >&2
  exit 1
fi

connection_identity() {
  python3 - "$1" <<'PY'
import sys
from urllib.parse import unquote, urlparse

parsed = urlparse(sys.argv[1])
if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
    raise SystemExit("restore URLs must be PostgreSQL URLs with an explicit host")
database = unquote(parsed.path.lstrip("/"))
if not database or "/" in database:
    raise SystemExit("restore URLs must name exactly one database")
print(f"{parsed.hostname.lower()}:{parsed.port or 5432}/{database}")
PY
}

source_identity="$(connection_identity "$source_url")"
target_identity="$(connection_identity "$target_url")"
if [ "$source_identity" = "$target_identity" ]; then
  echo "refusing to restore over the configured source database: $source_identity" >&2
  exit 1
fi
if [ "$target_identity" != "$expected_target" ]; then
  echo "restore target identity mismatch: expected '$expected_target', resolved '$target_identity'" >&2
  exit 1
fi
if [ "$confirmation" != "DESTROY_AND_RESTORE $target_identity" ]; then
  echo "restore confirmation mismatch; expected: DESTROY_AND_RESTORE $target_identity" >&2
  exit 1
fi

live_database_identity() {
  psql "$1" --no-psqlrc --tuples-only --no-align --set=ON_ERROR_STOP=1 \
    --command="SELECT COALESCE(inet_server_addr()::text, 'local') || ':' || inet_server_port() || '/' || current_database()"
}
source_live_identity="$(live_database_identity "$source_url")"
target_live_identity="$(live_database_identity "$target_url")"
if [ "$source_live_identity" = "$target_live_identity" ]; then
  echo "refusing to restore over the live source database identity: $source_live_identity" >&2
  exit 1
fi

pg_restore --list "$backup" >/dev/null
pg_restore --dbname="$target_url" --clean --if-exists --no-owner --no-privileges "$backup"
python3 -m videosim migrate --database-url "$target_url"
psql "$target_url" --set=ON_ERROR_STOP=1 <<'SQL'
BEGIN;
UPDATE control_plane_state
SET generation = generation + 1, updated_at = clock_timestamp()
WHERE singleton = TRUE;
UPDATE leases
SET state = 'expired', expires_at = clock_timestamp()
WHERE state IN ('offered', 'active', 'draining');
COMMIT;
SQL
if [ "$broker_mode" = "empty" ]; then
  psql "$target_url" --set=ON_ERROR_STOP=1 <<'SQL'
UPDATE outbox
SET state = 'pending', attempts = 0, publisher_id = NULL, locked_at = NULL,
    published_at = NULL, broker_sequence = NULL, last_error = '',
    next_attempt_at = clock_timestamp()
WHERE state <> 'dead';
SQL
fi
python3 -m videosim migration-status --database-url "$target_url"
printf 'Restore completed with lease expiry fencing (%s broker): %s\n' "$broker_mode" "$target_identity"
