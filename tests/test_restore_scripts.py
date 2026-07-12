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
