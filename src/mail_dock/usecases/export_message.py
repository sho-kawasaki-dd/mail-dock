"""Export a verified stored EML to a user-selected destination."""

from __future__ import annotations

import os
import sys
import tempfile
from contextlib import suppress

from mail_dock.domain.attachments import Path
from mail_dock.domain.errors import StorageError
from mail_dock.domain.ports import BaseEmlStorage


def _resolve_parent(path: Path) -> Path:
    parent = path.parent
    if not parent.is_dir():
        raise StorageError("EML export destination directory does not exist")
    try:
        resolved_parent = parent.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise StorageError("EML export destination directory is unavailable") from error
    if not resolved_parent.is_dir():
        raise StorageError("EML export destination is not a directory")
    return resolved_parent


def _temporary_path(parent: Path, payload: bytes) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".mail-dock-export-",
        suffix=".tmp",
        dir=parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as temporary_file:
            temporary_file.write(payload)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
    except BaseException:
        with suppress(OSError):
            temporary_path.unlink(missing_ok=True)
        raise
    return temporary_path


def _fsync_directory(path: Path) -> None:
    if sys.platform == "win32":
        return
    directory_fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def export_eml(
    storage: BaseEmlStorage,
    *,
    relative_path: str,
    expected_hash: str,
    dest_path: Path,
) -> Path:
    """Export one EML after complete hash verification.

    The temporary file is created beside the final destination so that the
    final ``os.replace`` remains atomic and stays on one filesystem volume.
    """

    raw = storage.read_verified(relative_path, expected_hash)
    destination_path = dest_path.expanduser()
    expected_parent = _resolve_parent(destination_path)
    if not destination_path.name:
        raise StorageError("EML export destination must name a file")

    temporary_path: Path | None = None
    try:
        current_parent = _resolve_parent(destination_path)
        if current_parent != expected_parent:
            raise StorageError("EML export destination directory changed")
        destination = current_parent / destination_path.name
        temporary_path = _temporary_path(current_parent, raw)
        os.replace(temporary_path, destination)  # noqa: PTH105
        temporary_path = None
        _fsync_directory(current_parent)
    except OSError as error:
        raise StorageError("Could not export EML") from error
    finally:
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)

    return destination
