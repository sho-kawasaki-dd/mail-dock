"""Probe storage capabilities without claiming to prove storage safety.

The probe measures capabilities rather than detecting product names. It is a
compatibility probe for known incompatibilities, not a complete safety proof,
and writes only below ``root/tmp/``.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, cast

from mail_dock.domain.errors import StorageDetachedError
from mail_dock.domain.ports import JSONValue
from mail_dock.infrastructure.storage.detach import storage_io


class CapabilityLevel(StrEnum):
    """Aggregate result of the storage capability probe."""

    OK = "ok"
    DEGRADED = "degraded"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class StorageCapabilities:
    """Measured storage capabilities for one path and one probe run."""

    exclusive_lock: bool
    replace_overwrite: bool
    wal_supported: bool
    fsync_supported: bool
    case_sensitive: bool
    long_path_ok: bool
    checked_at: str

    def as_dict(self) -> dict[str, JSONValue]:
        """Return a JSON-compatible representation of the measurements."""

        return {
            "exclusive_lock": self.exclusive_lock,
            "replace_overwrite": self.replace_overwrite,
            "wal_supported": self.wal_supported,
            "fsync_supported": self.fsync_supported,
            "case_sensitive": self.case_sensitive,
            "long_path_ok": self.long_path_ok,
            "checked_at": self.checked_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> StorageCapabilities | None:
        """Decode validated measurements, returning ``None`` for bad data."""

        boolean_fields = (
            "exclusive_lock",
            "replace_overwrite",
            "wal_supported",
            "fsync_supported",
            "case_sensitive",
            "long_path_ok",
        )
        if not all(isinstance(value.get(field), bool) for field in boolean_fields):
            return None
        checked_at = value.get("checked_at")
        if not isinstance(checked_at, str):
            return None
        try:
            parsed = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
            return None
        return cls(
            exclusive_lock=cast(bool, value["exclusive_lock"]),
            replace_overwrite=cast(bool, value["replace_overwrite"]),
            wal_supported=cast(bool, value["wal_supported"]),
            fsync_supported=cast(bool, value["fsync_supported"]),
            case_sensitive=cast(bool, value["case_sensitive"]),
            long_path_ok=cast(bool, value["long_path_ok"]),
            checked_at=checked_at,
        )


_CHILD_LOCK_SCRIPT = """
import os
import sys

path = sys.argv[1]
with open(path, "r+b") as handle:
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit(1)
raise SystemExit(0)
"""


def _run_io_probe(operation: Callable[[], bool]) -> bool:
    try:
        with storage_io():
            return operation()
    except (OSError, sqlite3.Error, subprocess.SubprocessError):
        return False


def _lock_probe_file(handle: BinaryIO) -> None:
    file_descriptor = handle.fileno()
    if os.name == "nt":
        import msvcrt

        locking_name = "locking"
        mode_name = "LK_NBLCK"
        locking = getattr(msvcrt, locking_name)
        mode = getattr(msvcrt, mode_name)
        handle.seek(0)
        locking(file_descriptor, mode, 1)
        return
    import fcntl

    fcntl.flock(file_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_probe_file(handle: BinaryIO) -> None:
    if os.name != "nt":
        return
    import msvcrt

    locking_name = "locking"
    mode_name = "LK_UNLCK"
    locking = getattr(msvcrt, locking_name)
    mode = getattr(msvcrt, mode_name)
    handle.seek(0)
    locking(handle.fileno(), mode, 1)


def _probe_exclusive_lock(lock_path: Path) -> bool:
    def operation() -> bool:
        lock_path.touch()
        handle = lock_path.open("r+b")
        process: subprocess.Popen[bytes] | None = None
        locked = False
        try:
            _lock_probe_file(handle)
            locked = True
            process = subprocess.Popen([sys.executable, "-c", _CHILD_LOCK_SCRIPT, str(lock_path)])
            try:
                return process.wait(timeout=2) != 0
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                return False
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
            if locked:
                _unlock_probe_file(handle)
            handle.close()

    return _run_io_probe(operation)


def _probe_replace_overwrite(source_path: Path, target_path: Path) -> bool:
    def operation() -> bool:
        source_path.write_bytes(b"source")
        target_path.write_bytes(b"target")
        source_path.replace(target_path)
        return target_path.read_bytes() == b"source"

    return _run_io_probe(operation)


def _probe_wal(database_path: Path) -> bool:
    def operation() -> bool:
        connection = sqlite3.connect(database_path)
        try:
            result = connection.execute("PRAGMA journal_mode=wal").fetchone()
            return result is not None and str(result[0]).lower() == "wal"
        finally:
            connection.close()

    return _run_io_probe(operation)


def _probe_fsync(file_path: Path, tmp_dir: Path) -> bool:
    def operation() -> bool:
        with file_path.open("wb") as probe_file:
            probe_file.write(b"fsync")
            probe_file.flush()
            os.fsync(probe_file.fileno())
        if os.name != "nt":
            directory_fd = os.open(tmp_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return True

    return _run_io_probe(operation)


def _probe_case_sensitivity(first_path: Path, second_path: Path) -> bool:
    def operation() -> bool:
        first_path.write_text("upper", encoding="ascii")
        second_path.write_text("lower", encoding="ascii")
        return (
            first_path.exists()
            and second_path.exists()
            and first_path.read_text(encoding="ascii") == "upper"
            and second_path.read_text(encoding="ascii") == "lower"
        )

    return _run_io_probe(operation)


def _probe_long_path(path: Path) -> bool:
    def operation() -> bool:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"long path")
        return path.read_bytes() == b"long path"

    return _run_io_probe(operation)


def _prepare_tmp_dir(tmp_dir: Path) -> bool:
    def operation() -> bool:
        tmp_dir.mkdir(parents=True, exist_ok=True)
        return True

    return _run_io_probe(operation)


def _cleanup_probe_files(tmp_dir: Path, prefix: str) -> None:
    try:
        with storage_io():
            for path in tmp_dir.glob(f"{prefix}*"):
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
    except StorageDetachedError:
        raise
    except OSError:
        pass


def probe_capabilities(root: Path) -> StorageCapabilities:
    """Measure storage operations using only temporary files below ``root/tmp``."""

    tmp_dir = root / "tmp"
    prefix = f".captest-{uuid.uuid4().hex}"
    checked_at = datetime.now(UTC).isoformat()
    try:
        if not _prepare_tmp_dir(tmp_dir):
            return StorageCapabilities(False, False, False, False, False, False, checked_at)

        base_path = tmp_dir / prefix
        capabilities = StorageCapabilities(
            exclusive_lock=_probe_exclusive_lock(base_path.with_suffix(".lock")),
            replace_overwrite=_probe_replace_overwrite(
                base_path.with_suffix(".a"), base_path.with_suffix(".b")
            ),
            wal_supported=_probe_wal(base_path.with_suffix(".db")),
            fsync_supported=_probe_fsync(base_path.with_suffix(".fsync"), tmp_dir),
            case_sensitive=_probe_case_sensitivity(
                base_path.with_name(f"{prefix}A"), base_path.with_name(f"{prefix}a")
            ),
            long_path_ok=_probe_long_path(
                base_path / "eml" / ("a" * 255) / "2026" / "08" / ("a" * 32 + ".eml")
            ),
            checked_at=checked_at,
        )
        return capabilities
    finally:
        _cleanup_probe_files(tmp_dir, prefix)


def capability_level(capabilities: StorageCapabilities) -> CapabilityLevel:
    """Aggregate measurements into the safety level used by callers."""

    if not capabilities.exclusive_lock or not capabilities.replace_overwrite:
        return CapabilityLevel.UNSUPPORTED
    if not capabilities.wal_supported or not capabilities.fsync_supported:
        return CapabilityLevel.DEGRADED
    return CapabilityLevel.OK


def journal_mode_for(capabilities: StorageCapabilities, *, network_drive: bool) -> str:
    """Select SQLite journaling conservatively for the measured storage."""

    if network_drive or not capabilities.wal_supported:
        return "DELETE"
    return "WAL"


def _normalized_path(root: Path) -> str:
    try:
        with storage_io():
            return os.path.normcase(str(root.expanduser().resolve(strict=False)))
    except StorageDetachedError:
        raise
    except OSError:
        return os.path.normcase(str(root.absolute()))


def _windows_volume_serial(root: Path) -> int:
    volume_name = ctypes.create_unicode_buffer(261)
    filesystem_name = ctypes.create_unicode_buffer(261)
    serial_number = ctypes.c_ulong()
    maximum_component_length = ctypes.c_ulong()
    filesystem_flags = ctypes.c_ulong()
    windll_name = "windll"
    windll = getattr(ctypes, windll_name)
    get_last_error_name = "get_last_error"
    get_last_error = getattr(ctypes, get_last_error_name)
    # GetVolumeInformationW requires the volume's root path; an arbitrary subdirectory fails.
    volume_root = ctypes.create_unicode_buffer(261)
    if not windll.kernel32.GetVolumePathNameW(str(root), volume_root, len(volume_root)):
        raise OSError(get_last_error(), "GetVolumePathNameW failed")
    succeeded = windll.kernel32.GetVolumeInformationW(
        volume_root.value,
        volume_name,
        len(volume_name),
        ctypes.byref(serial_number),
        ctypes.byref(maximum_component_length),
        ctypes.byref(filesystem_flags),
        filesystem_name,
        len(filesystem_name),
    )
    if not succeeded:
        raise OSError(get_last_error(), "GetVolumeInformationW failed")
    return serial_number.value


def storage_fingerprint(root: Path) -> str:
    """Return a path-and-medium fingerprint without inferring encryption state."""

    normalized_path = _normalized_path(root)
    try:
        with storage_io():
            if os.name == "nt":
                medium_id = f"{_windows_volume_serial(root):08x}"
                return f"windows:{medium_id}:{normalized_path}"
            device_id = root.stat().st_dev
            return f"posix:{device_id}:{normalized_path}"
    except StorageDetachedError:
        raise
    except (OSError, AttributeError):
        return f"path:{normalized_path}"
