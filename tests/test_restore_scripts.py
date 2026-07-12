import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path("scripts/postgres-restore.sh")


class PostgresRestoreSafetyTest(unittest.TestCase):
    def run_restore(self, source, target, expected, confirmation, extra_environment=None):
        with tempfile.NamedTemporaryFile(suffix=".dump") as backup:
            environment = os.environ | {
                "VIDEOSIM_DATABASE_URL": source,
                "VIDEOSIM_RESTORE_DATABASE_URL": target,
                "VIDEOSIM_RESTORE_EXPECTED_TARGET": expected,
                "VIDEOSIM_RESTORE_CONFIRM": confirmation,
                "VIDEOSIM_RESTORE_BROKER_MODE": "retained",
            } | (extra_environment or {})
            return subprocess.run(
                ["bash", str(SCRIPT), backup.name],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )

    def test_restore_validates_secrets_before_destructive_restore_and_converges_after_migration(self):
        script = SCRIPT.read_text(encoding="utf-8")

        runtime_validate = script.index("scripts/postgres-runtime-role.sh validate")
        restore = script.index('pg_restore --list "$backup"')
        migration = script.index('python3 -m videosim migrate --database-url "$target_url"')
        runtime_prepare = script.index("scripts/postgres-runtime-role.sh prepare")
        runtime_grants = script.index("scripts/postgres-runtime-role.sh grant")

        self.assertLess(runtime_validate, restore)
        self.assertGreater(runtime_prepare, migration)
        self.assertGreater(runtime_grants, runtime_prepare)

    def test_reused_runtime_owner_password_stops_before_pg_restore(self):
        source = "postgresql://owner:owner-secret@source.example.test/source"
        target = "postgresql://owner:owner-secret@target.example.test/target"
        identity = "target.example.test:5432/target"
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory, "pg_restore_called")
            psql = Path(directory, "psql")
            psql.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  *source.example.test*) echo '10.0.0.1:5432/source' ;;\n"
                "  *) echo '10.0.0.2:5432/target' ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            psql.chmod(0o755)
            pg_restore = Path(directory, "pg_restore")
            pg_restore.write_text(
                "#!/bin/sh\nprintf called > \"$PG_RESTORE_MARKER\"\nexit 99\n",
                encoding="utf-8",
            )
            pg_restore.chmod(0o755)
            result = self.run_restore(
                source,
                target,
                identity,
                f"DESTROY_AND_RESTORE {identity}",
                {
                    "PATH": f"{directory}:{os.environ['PATH']}",
                    "PG_RESTORE_MARKER": str(marker),
                    "POSTGRES_APP_PASSWORD": "owner-secret",
                    "POSTGRES_PUBLISHER_PASSWORD": "publisher-secret",
                    "POSTGRES_PRUNER_PASSWORD": "pruner-secret",
                },
            )
            pg_restore_called = marker.exists()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must differ from the PostgreSQL owner password", result.stdout)
        self.assertFalse(pg_restore_called)

    def test_non_owner_target_credential_stops_before_pg_restore(self):
        source = "postgresql://owner:source-secret@source.example.test/source"
        target = "postgresql://not_owner:target-secret@target.example.test/target"
        identity = "target.example.test:5432/target"
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory, "pg_restore_called")
            psql = Path(directory, "psql")
            psql.write_text(
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *pg_has_role*) echo f ;;\n"
                "  *source.example.test*) echo '10.0.0.1:5432/source' ;;\n"
                "  *) echo '10.0.0.2:5432/target' ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            psql.chmod(0o755)
            pg_restore = Path(directory, "pg_restore")
            pg_restore.write_text(
                "#!/bin/sh\nprintf called > \"$PG_RESTORE_MARKER\"\nexit 99\n",
                encoding="utf-8",
            )
            pg_restore.chmod(0o755)
            result = self.run_restore(
                source,
                target,
                identity,
                f"DESTROY_AND_RESTORE {identity}",
                {
                    "PATH": f"{directory}:{os.environ['PATH']}",
                    "PG_RESTORE_MARKER": str(marker),
                    "POSTGRES_APP_PASSWORD": "app-secret",
                    "POSTGRES_PUBLISHER_PASSWORD": "publisher-secret",
                    "POSTGRES_PRUNER_PASSWORD": "pruner-secret",
                },
            )
            pg_restore_called = marker.exists()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("target credential must be a non-runtime PostgreSQL owner", result.stdout)
        self.assertFalse(pg_restore_called)

    def test_refuses_source_database_even_with_confirmation(self):
        identity = "db.example.test:5432/videosim"
        result = self.run_restore(
            "postgresql://user:one@db.example.test/videosim",
            "postgresql://user:two@db.example.test:5432/videosim",
            identity,
            f"DESTROY_AND_RESTORE {identity}",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to restore over", result.stdout)

    def test_refuses_alias_that_resolves_to_live_source_database(self):
        identity = "restore-alias.example.test:5432/videosim"
        with tempfile.TemporaryDirectory() as directory:
            psql = Path(directory, "psql")
            psql.write_text("#!/bin/sh\necho '10.0.0.8:5432/videosim'\n", encoding="utf-8")
            psql.chmod(0o755)
            result = self.run_restore(
                "postgresql://primary.example.test/videosim",
                "postgresql://restore-alias.example.test/videosim",
                identity,
                f"DESTROY_AND_RESTORE {identity}",
                {"PATH": f"{directory}:{os.environ['PATH']}"},
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("live source database identity", result.stdout)

    def test_refuses_unexpected_target_identity(self):
        result = self.run_restore(
            "postgresql://db.example.test/videosim",
            "postgresql://restore.example.test/videosim_restore",
            "other.example.test:5432/videosim_restore",
            "DESTROY_AND_RESTORE other.example.test:5432/videosim_restore",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("restore target identity mismatch", result.stdout)

    def test_requires_explicit_broker_recovery_mode(self):
        identity = "restore.example.test:5432/videosim_restore"
        result = self.run_restore(
            "postgresql://db.example.test/videosim",
            "postgresql://restore.example.test/videosim_restore",
            identity,
            f"DESTROY_AND_RESTORE {identity}",
            {"VIDEOSIM_RESTORE_BROKER_MODE": "guess"},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be empty or retained", result.stdout)

    def test_refuses_inexact_destructive_confirmation(self):
        identity = "restore.example.test:5432/videosim_restore"
        result = self.run_restore(
            "postgresql://db.example.test/videosim",
            "postgresql://restore.example.test/videosim_restore",
            identity,
            "yes",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("restore confirmation mismatch", result.stdout)


if __name__ == "__main__":
    unittest.main()
