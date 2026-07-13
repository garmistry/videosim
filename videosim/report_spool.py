from __future__ import annotations

import binascii
import json
import os
import stat
import time
import uuid
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class ReportSpoolError(RuntimeError):
    pass


class ReportSpoolFullError(ReportSpoolError):
    pass


class EncryptedReportSpool:
    def __init__(self, directory: str | Path, key_file: str | Path, max_bytes: int):
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("report spool max bytes must be a positive integer")
        self.directory = Path(directory)
        self.max_bytes = max_bytes
        self._prepare_directory()
        self._fernet = Fernet(self._read_key(Path(key_file)))
        for temporary in self.directory.glob(".tmp-*"):
            temporary.unlink()

    def _prepare_directory(self):
        if self.directory.is_symlink():
            raise ReportSpoolError("report spool directory must not be a symlink")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.directory.is_dir():
            raise ReportSpoolError("report spool path is not a directory")
        self.directory.chmod(0o700)

    @staticmethod
    def _read_key(path: Path) -> bytes:
        try:
            details = path.lstat()
        except OSError as exc:
            raise ReportSpoolError(f"cannot read report spool key file: {path}") from exc
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise ReportSpoolError("report spool key must be a regular file")
        if stat.S_IMODE(details.st_mode) & 0o077:
            raise ReportSpoolError("report spool key permissions must be 0600 or stricter")
        try:
            key = path.read_bytes().strip()
            Fernet(key)
        except (OSError, ValueError, binascii.Error) as exc:
            raise ReportSpoolError("report spool key is not a valid Fernet key") from exc
        return key

    def stats(self) -> dict:
        entries = self.entries()
        return {
            "queuedReports": len(entries),
            "bytes": sum(path.stat().st_size for path in entries),
            "maxBytes": self.max_bytes,
        }

    def entries(self) -> list[Path]:
        entries = sorted(self.directory.glob("*.report"))
        if any(path.is_symlink() or not path.is_file() for path in entries):
            raise ReportSpoolError("report spool contains a non-regular entry")
        return entries

    def enqueue(self, payload: dict) -> Path:
        if payload.get("apiVersion") != "videosim.worker/v2" or not payload.get(
            "reportId"
        ):
            raise ReportSpoolError("only identified worker API v2 reports can be spooled")
        plaintext = json.dumps(
            payload, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        encrypted = self._fernet.encrypt(plaintext)
        if self.stats()["bytes"] + len(encrypted) > self.max_bytes:
            raise ReportSpoolFullError("encrypted report spool byte quota exceeded")

        # ponytail: one worker process owns a spool; add cross-process locking only
        # if shared spool ownership is introduced.
        temporary = self.directory / f".tmp-{uuid.uuid4()}"
        destination = self.directory / f"{time.time_ns():020d}-{uuid.uuid4()}.report"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(encrypted)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
            self._sync_directory()
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return destination

    def read(self, path: Path) -> dict:
        if path.parent != self.directory or path.suffix != ".report":
            raise ReportSpoolError("report spool entry is outside the configured directory")
        try:
            payload = json.loads(self._fernet.decrypt(path.read_bytes()))
        except (OSError, InvalidToken, json.JSONDecodeError) as exc:
            raise ReportSpoolError(f"cannot decrypt report spool entry: {path.name}") from exc
        if not isinstance(payload, dict):
            raise ReportSpoolError(f"report spool entry is not an object: {path.name}")
        return payload

    def acknowledge(self, path: Path):
        path.unlink()
        self._sync_directory()

    def _sync_directory(self):
        descriptor = os.open(self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
