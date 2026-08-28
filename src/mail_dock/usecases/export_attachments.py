"""Export verified message attachments without overwriting local files."""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from mail_dock.domain.attachments import Path, resolve_within, sanitize_attachment_name
from mail_dock.domain.errors import StorageError
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.messages import MessagePart
from mail_dock.domain.ports import BaseEmlStorage, BaseMessageRenderer
from mail_dock.domain.repository import MessageRecord
from mail_dock.domain.search import MessageDetail


@dataclass(frozen=True)
class ExportAttachmentWarning:
    """A warning associated with one exported attachment."""

    message_id: Any
    path: Path
    filename: str
    codes: tuple[str, ...]


@dataclass(frozen=True)
class ExportAttachmentsProgress:
    """Progress reported after each requested message is considered."""

    processed_count: int
    total_count: int
    exported_count: int
    skipped_count: int
    current_message_id: Any


@dataclass(frozen=True)
class ExportAttachmentsResult:
    """Outcome of an attachment export run."""

    files: tuple[Path, ...] = ()
    skipped_count: int = 0
    warnings: tuple[str, ...] = ()
    warning_details: tuple[ExportAttachmentWarning, ...] = ()
    executable_paths: tuple[Path, ...] = ()

    @property
    def exported_paths(self) -> tuple[Path, ...]:
        """Return the paths published by this export."""

        return self.files

    @property
    def exported_count(self) -> int:
        """Return the number of published attachments."""

        return len(self.files)

    @property
    def warning_count(self) -> int:
        """Return the number of attachments that produced warnings."""

        return len(self.warning_details)


def _resolve_destination_dir(dest_dir: Path) -> Path:
    if not dest_dir.is_dir():
        raise StorageError("Attachment destination directory does not exist")
    try:
        resolved = dest_dir.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise StorageError("Attachment destination directory is unavailable") from error
    if not resolved.is_dir():
        raise StorageError("Attachment destination is not a directory")
    return resolved


def _message_value(message: MessageRecord | MessageDetail, key: str) -> object:
    if isinstance(message, Mapping):
        return message.get(key)
    return getattr(message, key, None)


def _message_storage(message: MessageRecord | MessageDetail) -> tuple[str, str] | None:
    if _message_value(message, "local_state") == "purged":
        return None
    relative_path = _message_value(message, "relative_path")
    file_hash = _message_value(message, "file_hash")
    if not isinstance(relative_path, str) or not relative_path:
        raise StorageError("Message has no stored EML path")
    if not isinstance(file_hash, str) or not file_hash:
        raise StorageError("Message has no stored EML hash")
    return relative_path, file_hash


def _occupied(path: Path) -> bool:
    """Treat broken symlinks as occupied so they cannot be replaced."""

    return path.exists() or path.is_symlink()


def _candidate_path(dest_dir: Path, filename: str) -> Path:
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    counter = 0
    while True:
        candidate_name = filename if counter == 0 else f"{stem} ({counter}){suffix}"
        try:
            candidate = resolve_within(dest_dir, candidate_name)
        except ValueError as error:
            raise StorageError("Attachment destination is no longer safe") from error
        if not _occupied(candidate):
            return candidate
        counter += 1


def _fsync_directory(path: Path) -> None:
    if sys.platform == "win32":
        return
    directory_fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_without_overwrite(destination_dir: Path, destination: Path, payload: bytes) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".mail-dock-attachment-",
        suffix=".tmp",
        dir=destination_dir,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as temporary_file:
            temporary_file.write(payload)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        try:
            os.link(temporary_path, destination)
        except FileExistsError:
            raise
        else:
            temporary_path.unlink(missing_ok=True)
            _fsync_directory(destination_dir)
            return destination
    except FileExistsError:
        raise
    except OSError as error:
        raise StorageError("Could not export attachment") from error
    finally:
        with suppress(OSError):
            temporary_path.unlink(missing_ok=True)


def _export_part(
    part: MessagePart,
    destination_dir: Path,
) -> tuple[Path, tuple[str, ...]]:
    sanitized = sanitize_attachment_name(part.filename or "")
    while True:
        destination = _candidate_path(destination_dir, sanitized.name)
        try:
            return _write_without_overwrite(
                destination_dir, destination, part.payload
            ), sanitized.warnings
        except FileExistsError:
            # Another exporter won the name between resolve_within and publish.
            continue


def export_attachments(
    storage: BaseEmlStorage,
    renderer: BaseMessageRenderer,
    *,
    messages: Iterable[MessageRecord | MessageDetail],
    dest_dir: Path,
    cancel: CancelToken | None = None,
    on_progress: Callable[[ExportAttachmentsProgress], None] | None = None,
) -> ExportAttachmentsResult:
    """Export all non-inline attachments from the requested stored messages.

    EML bytes are integrity-checked before rendering. Each output is published
    from a temporary file in ``dest_dir`` using a no-overwrite link, so a
    collision or a concurrent exporter cannot replace an existing file.
    """

    destination_dir = _resolve_destination_dir(dest_dir)
    requested_messages = tuple(messages)
    token = cancel or CancelToken()
    files: list[Path] = []
    warning_codes: list[str] = []
    warning_details: list[ExportAttachmentWarning] = []
    executable_paths: list[Path] = []
    processed_count = 0
    skipped_count = 0

    for message in requested_messages:
        token.raise_if_cancelled()
        message_id = _message_value(message, "id")
        metadata = _message_storage(message)
        if metadata is None:
            skipped_count += 1
        else:
            relative_path, file_hash = metadata
            raw = storage.read_verified(relative_path, file_hash)
            rendered = renderer.render(raw)
            for part in rendered.parts:
                token.raise_if_cancelled()
                if part.content_id is not None or part.is_inline:
                    continue
                path, part_warnings = _export_part(
                    part=part,
                    destination_dir=destination_dir,
                )
                files.append(path)
                if part_warnings:
                    warning_codes.extend(part_warnings)
                    warning_details.append(
                        ExportAttachmentWarning(
                            message_id=message_id,
                            path=path,
                            filename=path.name,
                            codes=part_warnings,
                        )
                    )
                if "executable_extension" in part_warnings:
                    executable_paths.append(path)
        processed_count += 1
        if on_progress is not None:
            on_progress(
                ExportAttachmentsProgress(
                    processed_count=processed_count,
                    total_count=len(requested_messages),
                    exported_count=len(files),
                    skipped_count=skipped_count,
                    current_message_id=message_id,
                )
            )

    token.raise_if_cancelled()
    return ExportAttachmentsResult(
        files=tuple(files),
        skipped_count=skipped_count,
        warnings=tuple(warning_codes),
        warning_details=tuple(warning_details),
        executable_paths=tuple(executable_paths),
    )


__all__ = [
    "ExportAttachmentWarning",
    "ExportAttachmentsProgress",
    "ExportAttachmentsResult",
    "export_attachments",
]
