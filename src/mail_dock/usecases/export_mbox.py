"""Export stored EML messages to an atomically published mbox file."""

from __future__ import annotations

import mailbox
import os
import sys
import tempfile
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from mail_dock.domain.attachments import Path
from mail_dock.domain.errors import StorageError
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.ports import BaseEmlStorage
from mail_dock.domain.repository import BaseMessageRepository


@dataclass(frozen=True)
class ExportMboxProgress:
    """Progress reported after each requested message is considered."""

    processed_count: int
    total_count: int
    exported_count: int
    skipped_count: int
    current_message_id: Any


def _resolve_parent(path: Path) -> Path:
    parent = path.parent
    if not parent.is_dir():
        raise StorageError("mbox export destination directory does not exist")
    try:
        resolved_parent = parent.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise StorageError("mbox export destination directory is unavailable") from error
    if not resolved_parent.is_dir():
        raise StorageError("mbox export destination is not a directory")
    return resolved_parent


def _fsync_directory(path: Path) -> None:
    if sys.platform == "win32":
        return
    directory_fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _fsync_file(path: Path) -> None:
    file_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(file_fd)
    finally:
        os.close(file_fd)


def _new_temporary_path(parent: Path) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".mail-dock-mbox-",
        suffix=".tmp",
        dir=parent,
    )
    os.close(descriptor)
    return Path(temporary_name)


def _message_metadata(repo: BaseMessageRepository, message_id: Any) -> tuple[str, str] | None:
    message = repo.get_message(message_id)
    if message is None:
        raise StorageError(f"Message does not exist: {message_id}")
    if message.get("local_state") == "purged":
        return None
    relative_path = message.get("relative_path")
    file_hash = message.get("file_hash")
    if not isinstance(relative_path, str) or not relative_path:
        raise StorageError(f"Message has no stored EML path: {message_id}")
    if not isinstance(file_hash, str) or not file_hash:
        raise StorageError(f"Message has no stored EML hash: {message_id}")
    return relative_path, file_hash


def _append_message(mbox_file: mailbox.mbox, raw: bytes) -> None:
    try:
        mbox_file.add(raw)
    except (OSError, TypeError, ValueError) as error:
        raise StorageError("Could not append EML to mbox") from error


def export_mbox(
    repo: BaseMessageRepository,
    storage: BaseEmlStorage,
    *,
    message_ids: Iterable[Any],
    dest_path: Path,
    cancel: CancelToken | None = None,
    on_progress: Callable[[ExportMboxProgress], None] | None = None,
) -> Path:
    """Export verified messages to ``dest_path`` and return the published path.

    Only one EML is held at a time. Purged messages are omitted and reported
    through ``ExportMboxProgress.skipped_count``. A failed or cancelled export
    leaves neither a destination mbox nor its temporary file behind.
    """

    destination_path = dest_path.expanduser()
    if not destination_path.name:
        raise StorageError("mbox export destination must name a file")
    expected_parent = _resolve_parent(destination_path)
    requested_ids = tuple(message_ids)
    token = cancel or CancelToken()

    temporary_path: Path | None = None
    mbox_file: mailbox.mbox | None = None
    processed_count = 0
    exported_count = 0
    skipped_count = 0
    try:
        if _resolve_parent(destination_path) != expected_parent:
            raise StorageError("mbox export destination directory changed")
        temporary_path = _new_temporary_path(expected_parent)
        mbox_file = mailbox.mbox(str(temporary_path), create=True)
        for message_id in requested_ids:
            token.raise_if_cancelled()
            metadata = _message_metadata(repo, message_id)
            if metadata is None:
                skipped_count += 1
            else:
                relative_path, file_hash = metadata
                raw = storage.read_verified(relative_path, file_hash)
                _append_message(mbox_file, raw)
                exported_count += 1
            processed_count += 1
            if on_progress is not None:
                on_progress(
                    ExportMboxProgress(
                        processed_count=processed_count,
                        total_count=len(requested_ids),
                        exported_count=exported_count,
                        skipped_count=skipped_count,
                        current_message_id=message_id,
                    )
                )
        token.raise_if_cancelled()
        mbox_file.flush()
        mbox_file.close()
        mbox_file = None
        _fsync_file(temporary_path)

        current_parent = _resolve_parent(destination_path)
        if current_parent != expected_parent:
            raise StorageError("mbox export destination directory changed")
        destination = current_parent / destination_path.name
        os.replace(temporary_path, destination)  # noqa: PTH105
        temporary_path = None
        _fsync_directory(current_parent)
        return destination
    except StorageError:
        raise
    except OSError as error:
        raise StorageError("Could not export mbox") from error
    finally:
        if mbox_file is not None:
            with suppress(OSError):
                mbox_file.close()
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)


__all__ = ["ExportMboxProgress", "export_mbox"]
