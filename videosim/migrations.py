from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path


MIGRATION_PATTERN = re.compile(r"^(\d{3,})_([a-z0-9_]+)\.sql$")
DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
MIGRATION_LOCK_ID = 8_642_091_337


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path
    checksum: str
    sql: str


@dataclass(frozen=True)
class MigrationState:
    version: int
    name: str
    checksum: str
    applied: bool


def discover_migrations(directory: str | Path = DEFAULT_MIGRATIONS_DIR) -> list[Migration]:
    root = Path(directory)
    migrations = []
    for path in root.glob("*.sql"):
        match = MIGRATION_PATTERN.match(path.name)
        if not match:
            continue
        sql = path.read_text(encoding="utf-8")
        migrations.append(
            Migration(
                version=int(match.group(1)),
                name=match.group(2),
                path=path,
                checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
                sql=sql,
            )
        )
    migrations.sort(key=lambda migration: migration.version)
    versions = [migration.version for migration in migrations]
    if len(versions) != len(set(versions)):
        raise MigrationError("migration versions must be unique")
    return migrations


def _psycopg():
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - exercised by deployment configuration tests.
        raise MigrationError("psycopg is required for PostgreSQL migrations; install requirements.txt") from exc
    return psycopg


class PostgresMigrator:
    def __init__(self, database_url: str, migrations_dir: str | Path = DEFAULT_MIGRATIONS_DIR):
        if not database_url:
            raise ValueError("database_url is required")
        self.database_url = database_url
        self.migrations = discover_migrations(migrations_dir)

    def apply(self) -> list[MigrationState]:
        psycopg = _psycopg()
        with psycopg.connect(self.database_url) as connection:
            with connection.transaction():
                connection.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_ID,))
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version BIGINT PRIMARY KEY,
                        name TEXT NOT NULL,
                        checksum TEXT NOT NULL CHECK (length(checksum) = 64),
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
                    )
                    """
                )
                applied = {
                    int(row[0]): (str(row[1]), str(row[2]))
                    for row in connection.execute(
                        "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
                    ).fetchall()
                }
                known = {migration.version for migration in self.migrations}
                unknown = sorted(set(applied) - known)
                if unknown:
                    raise MigrationError(f"database contains unknown migration versions: {unknown}")
                result = []
                pending_seen = False
                for migration in self.migrations:
                    existing = applied.get(migration.version)
                    if existing:
                        if pending_seen:
                            raise MigrationError(
                                f"migration {migration.version} is applied after a missing earlier version"
                            )
                        if existing != (migration.name, migration.checksum):
                            raise MigrationError(f"migration {migration.version} checksum or name changed after application")
                        result.append(MigrationState(migration.version, migration.name, migration.checksum, True))
                        continue
                    pending_seen = True
                    connection.execute(migration.sql)
                    connection.execute(
                        "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                        (migration.version, migration.name, migration.checksum),
                    )
                    result.append(MigrationState(migration.version, migration.name, migration.checksum, True))
                return result

    def status(self) -> list[MigrationState]:
        psycopg = _psycopg()
        with psycopg.connect(self.database_url) as connection:
            table_exists = connection.execute(
                "SELECT to_regclass('public.schema_migrations') IS NOT NULL"
            ).fetchone()[0]
            applied = {}
            if table_exists:
                applied = {
                    int(row[0]): (str(row[1]), str(row[2]))
                    for row in connection.execute(
                        "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
                    ).fetchall()
                }
        known = {migration.version: migration for migration in self.migrations}
        unknown = sorted(set(applied) - set(known))
        if unknown:
            raise MigrationError(f"database contains unknown migration versions: {unknown}")
        for version, (name, checksum) in applied.items():
            migration = known[version]
            if (name, checksum) != (migration.name, migration.checksum):
                raise MigrationError(
                    f"migration {version} checksum or name changed after application"
                )
        return [
            MigrationState(
                migration.version,
                migration.name,
                migration.checksum,
                applied.get(migration.version) == (migration.name, migration.checksum),
            )
            for migration in self.migrations
        ]
