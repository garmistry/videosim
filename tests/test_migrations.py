import tempfile
import unittest
from pathlib import Path

from videosim.migrations import MigrationError, discover_migrations


class MigrationDiscoveryTest(unittest.TestCase):
    def test_discovers_ordered_up_migrations_and_ignores_down_files(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "002_second.sql").write_text("SELECT 2;", encoding="utf-8")
            Path(directory, "001_first.sql").write_text("SELECT 1;", encoding="utf-8")
            Path(directory, "001_first.down.sql").write_text("SELECT 0;", encoding="utf-8")

            migrations = discover_migrations(directory)

        self.assertEqual([item.version for item in migrations], [1, 2])
        self.assertEqual([item.name for item in migrations], ["first", "second"])
        self.assertEqual(len(migrations[0].checksum), 64)

    def test_orders_versions_numerically_across_filename_widths(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "1000_later.sql").write_text("SELECT 1000;", encoding="utf-8")
            Path(directory, "999_earlier.sql").write_text("SELECT 999;", encoding="utf-8")

            migrations = discover_migrations(directory)

        self.assertEqual([item.version for item in migrations], [999, 1000])

    def test_rejects_duplicate_numeric_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "001_first.sql").write_text("SELECT 1;", encoding="utf-8")
            Path(directory, "001_other.sql").write_text("SELECT 2;", encoding="utf-8")

            with self.assertRaises(MigrationError):
                discover_migrations(directory)


if __name__ == "__main__":
    unittest.main()
