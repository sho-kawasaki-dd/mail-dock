"""Atomic persistence and integrity-checked access for raw EML files.

EML placement is owned by this module. The temporary file is always created
under the storage root so that ``os.replace`` stays on the same volume as the
final EML. The EML and its manifest are the durable source of truth; the
metadata database is populated only after those writes have completed.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from mail_dock.domain.accounts import validate_account_id
from mail_dock.domain.errors import StorageError
from mail_dock.domain.messages import StoredEml
from mail_dock.domain.ports import BaseEmlStorage
from mail_dock.infrastructure.storage.detach import storage_io

_HASH_LENGTH = hashlib.sha256().digest_size * 2

__all__ = ["validate_account_id"]


def _fsync_directory(path: Path) -> None:
    """Durably publish a renamed file on POSIX filesystems."""

    if sys.platform == "win32":
        return
    directory_fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _date_directory(internal_date: datetime | None) -> str:
    if internal_date is None:
        return "unknown"
    if internal_date.tzinfo is not None:
        internal_date = internal_date.astimezone(UTC)
    return f"{internal_date.year:04d}/{internal_date.month:02d}"


def _relative_path(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _resolve_inside(root: Path, relative_path: str) -> Path:
    resolved_root = root.resolve()
    candidate = (resolved_root / Path(relative_path)).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as error:
        raise ValueError("EML path escapes the storage root") from error
    if candidate == resolved_root:
        raise ValueError("EML path must name a file below the storage root")
    return candidate


def _find_existing_eml(root: Path, account_id: str, file_hash: str) -> StoredEml | None:
    """Find and fully verify a same-account EML with the requested hash."""

    account_root = root / "eml" / account_id
    if not account_root.is_dir():
        return None
    filename = f"{file_hash[:32]}.eml"
    for candidate in account_root.rglob(filename):
        if not candidate.is_file():
            continue
        candidate_hash, size_bytes = _hash_file(candidate)
        if candidate_hash == file_hash:
            return StoredEml(
                _relative_path(root, candidate),
                file_hash,
                size_bytes,
                deduplicated=True,
            )
    return None


def save_eml(
    root: Path,
    account_id: str,
    internal_date: datetime | None,
    raw: bytes,
) -> StoredEml:
    """Save raw EML bytes atomically and return their durable storage record."""

    validate_account_id(account_id)
    root = root.expanduser().resolve()
    file_hash = hashlib.sha256(raw).hexdigest()
    date_directory = _date_directory(internal_date)
    destination = root / "eml" / account_id / date_directory / f"{file_hash[:32]}.eml"
    temporary_path: Path | None = None

    try:
        with storage_io():
            existing = _find_existing_eml(root, account_id, file_hash)
            if existing is not None:
                return existing
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.is_file():
                existing_hash, existing_size = _hash_file(destination)
                if existing_hash == file_hash:
                    return StoredEml(
                        _relative_path(root, destination),
                        file_hash,
                        existing_size,
                        deduplicated=True,
                    )

            temporary_path = root / "tmp" / f"{uuid.uuid4()}.eml"
            temporary_path.parent.mkdir(parents=True, exist_ok=True)
            with temporary_path.open("wb") as temporary_file:
                temporary_file.write(raw)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, destination)  # noqa: PTH105
            temporary_path = None
            _fsync_directory(destination.parent)
    finally:
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)

    return StoredEml(_relative_path(root, destination), file_hash, len(raw))


def cleanup_tmp(root: Path) -> int:
    """Remove abandoned temporary EML files, leaving ``tmp/pstimp`` intact."""

    temporary_root = root.expanduser().resolve() / "tmp"
    removed_files = 0
    with storage_io():
        if not temporary_root.is_dir():
            return 0
        for child in temporary_root.iterdir():
            if child.name == "pstimp":
                continue
            if child.is_dir() and not child.is_symlink():
                removed_files += sum(1 for path in child.rglob("*") if path.is_file())
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)
                removed_files += 1
    return removed_files


class EmlStorage(BaseEmlStorage):
    """Filesystem implementation of the atomic EML storage port."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()

    def save(self, account_id: str, internal_date: datetime | None, raw: bytes) -> StoredEml:
        return save_eml(self.root, account_id, internal_date, raw)

    def reuse(self, relative_path: str, expected_hash: str) -> StoredEml | None:
        if len(expected_hash) != _HASH_LENGTH or any(
            character not in "0123456789abcdefABCDEF" for character in expected_hash
        ):
            return None
        with storage_io():
            path = _resolve_inside(self.root, relative_path)
            if not path.is_file():
                return None
            file_hash, size_bytes = _hash_file(path)
        if file_hash != expected_hash.casefold():
            return None
        return StoredEml(relative_path, file_hash, size_bytes, deduplicated=True)

    def read(self, relative_path: str) -> bytes:
        with storage_io():
            path = _resolve_inside(self.root, relative_path)
            return path.read_bytes()

    def read_verified(self, relative_path: str, expected_hash: str) -> bytes:
        if len(expected_hash) != _HASH_LENGTH or any(
            character not in "0123456789abcdefABCDEF" for character in expected_hash
        ):
            raise StorageError("Invalid expected EML hash")

        with storage_io():
            path = _resolve_inside(self.root, relative_path)
            digest = hashlib.sha256()
            payload = bytearray()
            with path.open("rb") as file:
                for chunk in iter(lambda: file.read(1024 * 1024), b""):
                    digest.update(chunk)
                    payload.extend(chunk)

        actual_hash = digest.hexdigest()
        if actual_hash != expected_hash.casefold():
            raise StorageError("EML file hash does not match expected hash")
        return bytes(payload)
