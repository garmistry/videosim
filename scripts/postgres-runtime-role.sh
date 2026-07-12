#!/bin/sh
set -eu

mode=${1:-prepare}
case "$mode" in
  prepare|grant|validate) ;;
  *)
    echo "usage: $0 [validate|prepare|grant]" >&2
    exit 2
    ;;
esac

runtime_database_url=${POSTGRES_RUNTIME_DATABASE_URL:-}
if [ -n "$runtime_database_url" ]; then
  owner_password=${POSTGRES_RUNTIME_OWNER_PASSWORD:-}
  if [ -z "$owner_password" ]; then
    owner_password="$(python3 - "$runtime_database_url" <<'PY'
from urllib.parse import unquote, urlparse
import sys

parsed = urlparse(sys.argv[1])
if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
    raise SystemExit("POSTGRES_RUNTIME_DATABASE_URL must be an explicit PostgreSQL URL")
if not parsed.username or parsed.password is None or not unquote(parsed.password):
    raise SystemExit(
        "set POSTGRES_RUNTIME_OWNER_PASSWORD or include owner credentials in POSTGRES_RUNTIME_DATABASE_URL"
    )
print(unquote(parsed.password))
PY
)"
  fi
else
  : "${PGHOST:?PGHOST is required}"
  : "${PGPORT:=5432}"
  : "${PGDATABASE:?PGDATABASE is required}"
  : "${PGUSER:?PGUSER is required}"
  : "${PGPASSWORD:?PGPASSWORD is required}"
  owner_password=$PGPASSWORD
fi
: "${POSTGRES_APP_PASSWORD:?POSTGRES_APP_PASSWORD is required}"
: "${POSTGRES_PUBLISHER_PASSWORD:?POSTGRES_PUBLISHER_PASSWORD is required}"
: "${POSTGRES_PRUNER_PASSWORD:?POSTGRES_PRUNER_PASSWORD is required}"

run_psql() {
  if [ -n "$runtime_database_url" ]; then
    psql "$runtime_database_url" "$@"
  else
    psql "$@"
  fi
}

for password_name in \
  POSTGRES_APP_PASSWORD \
  POSTGRES_PUBLISHER_PASSWORD \
  POSTGRES_PRUNER_PASSWORD
do
  case "$password_name" in
    POSTGRES_APP_PASSWORD) password=$POSTGRES_APP_PASSWORD ;;
    POSTGRES_PUBLISHER_PASSWORD) password=$POSTGRES_PUBLISHER_PASSWORD ;;
    POSTGRES_PRUNER_PASSWORD) password=$POSTGRES_PRUNER_PASSWORD ;;
  esac
  case "$password" in
    *[!A-Za-z0-9._~-]*|'')
      echo "$password_name must be non-empty and URL-safe (A-Z, a-z, 0-9, ., _, ~, -)" >&2
      exit 2
      ;;
  esac
done

if [ "$POSTGRES_APP_PASSWORD" = "$POSTGRES_PUBLISHER_PASSWORD" ] \
  || [ "$POSTGRES_APP_PASSWORD" = "$POSTGRES_PRUNER_PASSWORD" ] \
  || [ "$POSTGRES_PUBLISHER_PASSWORD" = "$POSTGRES_PRUNER_PASSWORD" ]; then
  echo "runtime role passwords must be distinct" >&2
  exit 2
fi
if [ "$POSTGRES_APP_PASSWORD" = "$owner_password" ] \
  || [ "$POSTGRES_PUBLISHER_PASSWORD" = "$owner_password" ] \
  || [ "$POSTGRES_PRUNER_PASSWORD" = "$owner_password" ]; then
  echo "runtime role passwords must differ from the PostgreSQL owner password" >&2
  exit 2
fi

validate_owner_authority() {
  # This is deliberately read-only so restore can prove the credential is a
  # migration owner before pg_restore --clean has any chance to mutate data.
  authority=$(run_psql -X -At -v ON_ERROR_STOP=1 -c "
    SELECT CASE
      WHEN current_user = ANY (ARRAY['videosim_app', 'videosim_publisher', 'videosim_pruner'])
        THEN false
      WHEN (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)
        THEN true
      WHEN EXISTS (
        SELECT 1
        FROM pg_database AS database
        WHERE database.datname = current_database()
          AND pg_has_role(current_user, database.datdba, 'USAGE')
      ) THEN true
      ELSE false
    END;
  ")
  if [ "$authority" != "t" ]; then
    echo "target credential must be a non-runtime PostgreSQL owner or superuser" >&2
    exit 2
  fi
}

case "$mode" in
  validate)
    validate_owner_authority
    ;;
  prepare)
    validate_owner_authority
    # Before migrations, converge the fixed runtime roles: remove memberships
    # and direct grants, reassign accidentally owned objects, and reset
    # privileged attributes. This intentionally fails closed if PGUSER lacks
    # the privileges to do that work.
    run_psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$
DECLARE
    role_names text[] := ARRAY['videosim_app', 'videosim_publisher', 'videosim_pruner'];
    role_passwords text[] := ARRAY[
        '${POSTGRES_APP_PASSWORD}',
        '${POSTGRES_PUBLISHER_PASSWORD}',
        '${POSTGRES_PRUNER_PASSWORD}'
    ];
    role_name text;
    role_password text;
    parent_role text;
    member_role text;
    role_oid oid;
    role_index integer;
BEGIN
    IF current_user = ANY(role_names) THEN
        RAISE EXCEPTION 'PGUSER must be a migration owner, not a VideoSim runtime role';
    END IF;

    FOR role_index IN 1..array_length(role_names, 1) LOOP
        role_name := role_names[role_index];
        role_password := role_passwords[role_index];
        SELECT oid INTO role_oid FROM pg_roles WHERE rolname = role_name;

        IF role_oid IS NOT NULL THEN
            -- A runtime role must never remain an owner of this database or
            -- its public schema, even after a failed/older deployment.
            IF EXISTS (
                SELECT 1 FROM pg_database
                WHERE datname = current_database() AND datdba = role_oid
            ) THEN
                EXECUTE format('ALTER DATABASE %I OWNER TO %I', current_database(), current_user);
            END IF;
            IF EXISTS (
                SELECT 1 FROM pg_namespace
                WHERE nspname = 'public' AND nspowner = role_oid
            ) THEN
                EXECUTE format('ALTER SCHEMA public OWNER TO %I', current_user);
            END IF;
            -- Remove both directions: a parent role lets this runtime role
            -- assume extra privileges, while a member role can inherit this
            -- runtime role's rights.
            FOR parent_role IN
                SELECT parent.rolname
                FROM pg_auth_members AS membership
                JOIN pg_roles AS parent ON parent.oid = membership.roleid
                WHERE membership.member = role_oid
            LOOP
                EXECUTE format('REVOKE %I FROM %I', parent_role, role_name);
            END LOOP;
            FOR member_role IN
                SELECT child.rolname
                FROM pg_auth_members AS membership
                JOIN pg_roles AS child ON child.oid = membership.member
                WHERE membership.roleid = role_oid
            LOOP
                EXECUTE format('REVOKE %I FROM %I', role_name, member_role);
            END LOOP;
            EXECUTE format('REASSIGN OWNED BY %I TO %I', role_name, current_user);
            EXECUTE format(
                'REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM %I',
                role_name
            );
            EXECUTE format(
                'REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM %I',
                role_name
            );
            EXECUTE format(
                'REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM %I',
                role_name
            );
            EXECUTE format('REVOKE ALL PRIVILEGES ON SCHEMA public FROM %I', role_name);
        ELSE
            EXECUTE format(
                'CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT',
                role_name
            );
        END IF;

        EXECUTE format(
            'ALTER ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD %L',
            role_name,
            role_password
        );
    END LOOP;
END;
\$\$;
SQL
    ;;
  grant)
    # Migrations 004/005 own this idempotent grant policy. Run it after every
    # migration attempt so an already-applied migration still restores the
    # minimal rights removed by prepare during a restart.
    run_psql -v ON_ERROR_STOP=1 -c 'SELECT videosim_grant_runtime_roles();'
    ;;
esac
