import os
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from videosim.report_spool import (
    EncryptedReportSpool,
    ReportSpoolError,
    ReportSpoolFullError,
)


def report_payload():
    return {
        "apiVersion": "videosim.worker/v2",
        "reportId": "00000000-0000-0000-0000-000000000001",
        "workerId": "worker-a",
        "workerIncarnationId": "00000000-0000-0000-0000-000000000002",
        "sequence": 1,
        "streamIds": ["stream-1"],
        "leases": [],
        "state": {"marker": "plaintext-must-not-leak"},
    }


def key_file(directory: str) -> Path:
    path = Path(directory, "spool.key")
    path.write_bytes(Fernet.generate_key())
    path.chmod(0o600)
    return path


class ReportSpoolTest(unittest.TestCase):
    def test_report_is_encrypted_authenticated_and_persistent(self):
        with tempfile.TemporaryDirectory() as directory:
            key = key_file(directory)
            spool = EncryptedReportSpool(Path(directory, "reports"), key, 100_000)
            entry = spool.enqueue(report_payload())

            self.assertNotIn(b"plaintext-must-not-leak", entry.read_bytes())
            reopened = EncryptedReportSpool(Path(directory, "reports"), key, 100_000)
            self.assertEqual(reopened.read(entry), report_payload())

            encrypted = bytearray(entry.read_bytes())
            encrypted[-1] ^= 1
            entry.write_bytes(encrypted)
            with self.assertRaisesRegex(ReportSpoolError, "cannot decrypt"):
                reopened.read(entry)

    def test_spool_quota_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = EncryptedReportSpool(
                Path(directory, "reports"), key_file(directory), 1
            )

            with self.assertRaisesRegex(ReportSpoolFullError, "quota"):
                spool.enqueue(report_payload())
            self.assertEqual(spool.stats()["queuedReports"], 0)

    def test_key_file_must_not_be_group_or_world_accessible(self):
        with tempfile.TemporaryDirectory() as directory:
            key = key_file(directory)
            os.chmod(key, 0o644)

            with self.assertRaisesRegex(ReportSpoolError, "permissions"):
                EncryptedReportSpool(Path(directory, "reports"), key, 100_000)
