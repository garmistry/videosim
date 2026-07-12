#!/usr/bin/env bash
set -euo pipefail

output="${1:?usage: scripts/postgres-backup.sh OUTPUT.dump}"
database_url="${VIDEOSIM_DATABASE_URL:?VIDEOSIM_DATABASE_URL is required}"

umask 077
pg_dump --dbname="$database_url" --format=custom --compress=9 --file="$output"
pg_restore --list "$output" >/dev/null
printf 'PostgreSQL backup verified: %s\n' "$output"
