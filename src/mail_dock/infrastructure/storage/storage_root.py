"""Storage-root identification, layout management, and process locking.

``probe`` and ``resolve_root`` are deliberately read-only: a path is not
considered mail-dock storage merely because it exists.  The composition root
must explicitly call ``initialize_root`` before creating a new marker.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import platform
import shutil
import sys
import time
import uuid
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from mail_dock.domain.errors import (
    InsufficientSpaceError,
    StorageForeignRootError,
    StorageLockedError,
    StorageRootMissingError,
)
from mail_dock.infrastructure.storage.detach import classify_os_error

MARKER_FILENAME = ".maildock_root"
LOCK_FILENAME = ".lock"
LOCK_META_FILENAME = ".lock.meta.json"
MARKER_SCHEMA_VERSION = 1
APP_NAME = "mail-dock"
WARNING_FREE_BYTES = 20 * 1024**3
MINIMUM_FREE_BYTES = 5 * 1024**3
DEFAULT_HEARTBEAT_INTERVAL_SEC = 5
STALE_HEARTBEAT_MULTIPLIER = 2


class RootProbe(StrEnum):
    """Result of checking whether a path is a known storage root."""

    OK = "ok"
    MISSING = "missing"
    FOREIGN = "foreign"


class SpaceStatus(StrEnum):
    """Free-space status for a storage root."""

    OK = "ok"
    WARNING = "warning"


class DriveKind(StrEnum):
    """Physical type used to select safe SQLite journaling settings."""

    LOCAL = "local"
    NETWORK = "network"


@dataclass(frozen=True)
class RootMarker:
    """Validated contents of a ``.maildock_root`` marker file."""

    schema: int
    root_uuid: str
    created_at: str
    app: str = APP_NAME

    def as_dict(self) -> dict[str, object]:
        """Return the marker in its on-disk JSON representation."""

        return {
            "schema": self.schema,
            "root_uuid": self.root_uuid,
            "created_at": self.created_at,
            "app": self.app,
        }


@dataclass(frozen=True)
class RootResolution:
    """Path and probe result returned by root-candidate resolution."""

    path: Path | None
    probe: RootProbe


def _raise_storage_os_error(error: OSError) -> None:
    classified = classify_os_error(error)
    if classified is error:
        raise StorageRootMissingError(str(error)) from error
    raise classified from error


def _read_marker(path: Path) -> RootMarker:
    try:
        with path.open(encoding="utf-8") as marker_file:
            raw: Any = json.load(marker_file)
    except (OSError, json.JSONDecodeError) as error:
        _raise_storage_os_error(error) if isinstance(error, OSError) else None
        raise StorageForeignRootError(f"Invalid storage marker: {path}") from error

    if not isinstance(raw, dict):
        raise StorageForeignRootError(f"Invalid storage marker: {path}")
    schema = raw.get("schema")
    root_uuid = raw.get("root_uuid")
    created_at = raw.get("created_at")
    app = raw.get("app")
    if schema != MARKER_SCHEMA_VERSION or not isinstance(root_uuid, str):
        raise StorageForeignRootError(f"Invalid storage marker: {path}")
    if not isinstance(created_at, str) or not isinstance(app, str) or app != APP_NAME:
        raise StorageForeignRootError(f"Invalid storage marker: {path}")
    try:
        parsed_uuid = uuid.UUID(root_uuid)
        datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except (ValueError, TypeError) as error:
        raise StorageForeignRootError(f"Invalid storage marker: {path}") from error
    if parsed_uuid.version != 4:
        raise StorageForeignRootError(f"Invalid storage marker: {path}")
    return RootMarker(schema, root_uuid, created_at, app)


def _fsync_directory(path: Path) -> None:
    if sys.platform == "win32":
        return
    try:
        directory_fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def initialize_root(path: Path) -> RootMarker:
    """Read an existing marker or create and fsync a new root marker."""

    path = path.expanduser()
    try:
        if path.exists() and not path.is_dir():
            raise StorageRootMissingError(f"Storage root is not a directory: {path}")
        path.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        _raise_storage_os_error(error)

    marker_path = path / MARKER_FILENAME
    if marker_path.exists():
        return _read_marker(marker_path)

    marker = RootMarker(
        MARKER_SCHEMA_VERSION,
        str(uuid.uuid4()),
        datetime.now(UTC).isoformat(),
    )
    try:
        with marker_path.open("x", encoding="utf-8") as marker_file:
            json.dump(marker.as_dict(), marker_file, ensure_ascii=True, separators=(",", ":"))
            marker_file.write("\n")
            marker_file.flush()
            os.fsync(marker_file.fileno())
        _fsync_directory(path)
    except FileExistsError:
        return _read_marker(marker_path)
    except OSError as error:
        _raise_storage_os_error(error)
    return marker


def probe(path: Path, expected_uuid: str | None) -> RootProbe:
    """Probe a path without creating directories or writing a marker."""

    path = path.expanduser()
    try:
        if not path.is_dir():
            return RootProbe.MISSING
    except OSError as error:
        classified = classify_os_error(error)
        if classified is not error:
            raise classified from error
        return RootProbe.MISSING

    marker_path = path / MARKER_FILENAME
    try:
        if not marker_path.is_file():
            return RootProbe.MISSING
    except OSError as error:
        classified = classify_os_error(error)
        if classified is not error:
            raise classified from error
        return RootProbe.MISSING
    try:
        marker = _read_marker(marker_path)
    except StorageForeignRootError:
        return RootProbe.FOREIGN
    return (
        RootProbe.OK
        if expected_uuid is None or marker.root_uuid == expected_uuid
        else RootProbe.FOREIGN
    )


def _normalized_candidates(candidates: Sequence[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = candidate.expanduser().resolve(strict=False)
        key = os.path.normcase(str(normalized))
        if key not in seen:
            seen.add(key)
            result.append(normalized)
    return result


def resolve_root(candidates: Sequence[Path], expected_uuid: str | None) -> RootResolution:
    """Return the first matching candidate, without modifying any candidate."""

    foreign_path: Path | None = None
    for candidate in _normalized_candidates(candidates):
        candidate_probe = probe(candidate, expected_uuid)
        if candidate_probe is RootProbe.OK:
            return RootResolution(candidate, RootProbe.OK)
        if candidate_probe is RootProbe.FOREIGN and foreign_path is None:
            foreign_path = candidate
    # A foreign marker is more dangerous than a missing path: do not allow a
    # caller to silently initialize or write to a different physical device.
    if foreign_path is not None:
        return RootResolution(foreign_path, RootProbe.FOREIGN)
    return RootResolution(None, RootProbe.MISSING)


def ensure_layout(root: Path) -> None:
    """Create the directories required by the storage-root contract."""

    try:
        for relative_path in ("eml", "manifests/imap", "manifests/pst", "tmp", "logs"):
            (root / relative_path).mkdir(parents=True, exist_ok=True)
    except OSError as error:
        _raise_storage_os_error(error)
    # ``tmp`` must stay beside EML on the same volume so os.replace remains
    # atomic when a staged EML is moved into its final location.


def free_space(path: Path) -> int:
    """Return free bytes on the filesystem containing ``path``."""

    try:
        return shutil.disk_usage(path).free
    except OSError as error:
        _raise_storage_os_error(error)
    raise AssertionError("unreachable")


def check_free_space(path: Path) -> SpaceStatus:
    """Validate free space, warning below 20 GiB and stopping below 5 GiB."""

    available = free_space(path)
    if available < MINIMUM_FREE_BYTES:
        raise InsufficientSpaceError(
            f"Storage root has less than {MINIMUM_FREE_BYTES} free bytes: {available}"
        )
    return SpaceStatus.WARNING if available < WARNING_FREE_BYTES else SpaceStatus.OK


def drive_kind(path: Path) -> DriveKind:
    """Classify a Windows remote drive; non-Windows paths are local here."""

    if os.name != "nt":
        return DriveKind.LOCAL
    try:
        drive = os.path.splitdrive(str(path.resolve()))[0]
        if not drive:
            drive = str(path.resolve())
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return DriveKind.LOCAL
        drive_type = windll.kernel32.GetDriveTypeW(f"{drive}\\")
    except (OSError, AttributeError):
        return DriveKind.LOCAL
    return DriveKind.NETWORK if drive_type == 4 else DriveKind.LOCAL


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _heartbeat_is_fresh(value: object, now: datetime, stale_after_sec: float) -> bool:
    if not isinstance(value, str):
        return False
    try:
        heartbeat = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=UTC)
    return (now - heartbeat).total_seconds() <= stale_after_sec


class StorageLock:
    """Own the root lock and its separately stored heartbeat metadata."""

    def __init__(
        self,
        root: Path,
        *,
        heartbeat_interval_sec: int = DEFAULT_HEARTBEAT_INTERVAL_SEC,
        retry_attempts: int = 3,
        retry_delay_sec: float = 0.05,
    ) -> None:
        if heartbeat_interval_sec <= 0:
            raise ValueError("heartbeat_interval_sec must be positive")
        if retry_attempts <= 0:
            raise ValueError("retry_attempts must be positive")
        self.root = root
        self.heartbeat_interval_sec = heartbeat_interval_sec
        self.retry_attempts = retry_attempts
        self.retry_delay_sec = retry_delay_sec
        self._lock_path = root / LOCK_FILENAME
        self._meta_path = root / LOCK_META_FILENAME
        self._fd: int | None = None
        self._instance_uuid = str(uuid.uuid4())

    @property
    def held(self) -> bool:
        """Whether this instance currently owns the OS lock."""

        return self._fd is not None

    def _read_meta(self) -> dict[str, object] | None:
        try:
            with self._meta_path.open(encoding="utf-8") as meta_file:
                raw: Any = json.load(meta_file)
        except (OSError, json.JSONDecodeError):
            return None
        return raw if isinstance(raw, dict) else None

    def _write_meta(self) -> None:
        metadata = {
            "pid": os.getpid(),
            "instance_uuid": self._instance_uuid,
            "machine_id": platform.node(),
            "heartbeat_at": _utc_now().isoformat(),
        }
        temporary_path = self._meta_path.with_name(
            f".{self._meta_path.name}.{self._instance_uuid}.tmp"
        )
        try:
            with temporary_path.open("w", encoding="utf-8") as meta_file:
                json.dump(metadata, meta_file, separators=(",", ":"))
                meta_file.write("\n")
                meta_file.flush()
                os.fsync(meta_file.fileno())
            temporary_path.replace(self._meta_path)
        except OSError as error:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)
            _raise_storage_os_error(error)

    def _try_os_lock(self) -> bool:
        fd: int | None = None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                locking_name = "locking"
                mode_name = "LK_NBLCK"
                locking = getattr(msvcrt, locking_name)
                locking(fd, getattr(msvcrt, mode_name), 1)
                os.ftruncate(fd, 0)
            else:
                import fcntl

                flock_name = "flock"
                lock_ex_name = "LOCK_EX"
                lock_nb_name = "LOCK_NB"
                flock = getattr(fcntl, flock_name)
                lock_ex = getattr(fcntl, lock_ex_name)
                lock_nb = getattr(fcntl, lock_nb_name)
                flock(fd, lock_ex | lock_nb)
            os.ftruncate(fd, 0)
        except OSError as error:
            if fd is not None:
                os.close(fd)
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                return False
            classified = classify_os_error(error)
            if classified is error:
                raise StorageLockedError(str(error)) from error
            raise classified from error
        if fd is None:
            raise StorageLockedError(f"Could not open storage lock: {self.root}")
        self._fd = fd
        return True

    def acquire(self) -> StorageLock:
        """Acquire the OS lock, recovering only stale metadata after success."""

        for attempt in range(self.retry_attempts):
            if not self._try_os_lock():
                if attempt + 1 < self.retry_attempts:
                    time.sleep(self.retry_delay_sec)
                    continue
                raise StorageLockedError(f"Storage root is already locked: {self.root}")
            meta = self._read_meta()
            if meta is not None and _heartbeat_is_fresh(
                meta.get("heartbeat_at"),
                _utc_now(),
                self.heartbeat_interval_sec * STALE_HEARTBEAT_MULTIPLIER,
            ):
                self._release_os_lock(remove_files=False)
                if attempt + 1 < self.retry_attempts:
                    time.sleep(self.retry_delay_sec)
                    continue
                raise StorageLockedError(f"Storage lock metadata is active: {self.root}")
            self._write_meta()
            return self
        raise StorageLockedError(f"Could not acquire storage lock: {self.root}")

    def touch_heartbeat(self) -> None:
        """Persist a fresh heartbeat for the currently held lock."""

        if not self.held:
            raise StorageLockedError("Storage lock is not held by this instance")
        self._write_meta()

    def _release_os_lock(self, *, remove_files: bool) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                locking_name = "locking"
                mode_name = "LK_UNLCK"
                locking = getattr(msvcrt, locking_name)
                locking(fd, getattr(msvcrt, mode_name), 1)
            else:
                import fcntl

                flock_name = "flock"
                lock_un_name = "LOCK_UN"
                flock = getattr(fcntl, flock_name)
                lock_un = getattr(fcntl, lock_un_name)
                flock(fd, lock_un)
        finally:
            os.close(fd)
        if not remove_files:
            return
        try:
            self._lock_path.unlink(missing_ok=True)
            self._meta_path.unlink(missing_ok=True)
        except OSError as error:
            _raise_storage_os_error(error)

    def release(self) -> None:
        """Unlock and remove this instance's lock and metadata files."""

        self._release_os_lock(remove_files=True)

    def __enter__(self) -> StorageLock:
        return self.acquire()

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.release()
