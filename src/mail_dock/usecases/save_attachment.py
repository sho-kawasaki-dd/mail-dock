"""Prepare and atomically commit an attachment save."""

from __future__ import annotations

import os
import sys
import tempfile
from contextlib import suppress

from mail_dock.domain.attachments import Path, resolve_within, sanitize_attachment_name
from mail_dock.domain.errors import StorageError
from mail_dock.domain.messages import AttachmentSavePlan, MessagePart, SavedFile
from mail_dock.domain.ports import BaseEmlStorage, BaseMessageRenderer


def _part_at(rendered_parts: tuple[MessagePart, ...], part_index: int) -> MessagePart:
    if part_index < 0 or part_index >= len(rendered_parts):
        raise StorageError("Attachment part index is out of range")
    part = rendered_parts[part_index]
    if part.is_inline:
        raise StorageError("Inline MIME parts cannot be saved as attachments")
    return part


def _candidate_name(filename: str, dest_dir: Path) -> str:
    path = Path(filename)
    stem = path.stem
    suffix = path.suffix
    candidate = filename
    counter = 1
    while (dest_dir / candidate).exists():
        candidate = f"{stem} ({counter}){suffix}"
        counter += 1
    return candidate


def _prepare_part(
    storage: BaseEmlStorage,
    renderer: BaseMessageRenderer,
    *,
    relative_path: str,
    expected_hash: str,
    part_index: int,
    dest_dir: Path,
    filename: str | None,
) -> tuple[AttachmentSavePlan, MessagePart]:
    if not dest_dir.is_dir():
        raise StorageError("Attachment destination directory does not exist")
    resolved_dest_dir = dest_dir.expanduser().resolve()
    raw = storage.read_verified(relative_path, expected_hash)
    part = _part_at(renderer.render(raw).parts, part_index)
    sanitized = sanitize_attachment_name(filename if filename is not None else part.filename or "")
    chosen_name = _candidate_name(sanitized.name, resolved_dest_dir)
    plan = AttachmentSavePlan(
        relative_path=relative_path,
        expected_hash=expected_hash,
        part_index=part_index,
        dest_dir=resolved_dest_dir,
        filename=chosen_name,
        warnings=sanitized.warnings,
        is_executable=sanitized.is_executable,
    )
    return plan, part


def prepare_attachment_save(
    storage: BaseEmlStorage,
    renderer: BaseMessageRenderer,
    *,
    relative_path: str,
    expected_hash: str,
    part_index: int,
    dest_dir: Path,
    filename: str | None = None,
) -> AttachmentSavePlan:
    """Read and validate an attachment without creating a destination file."""

    plan, _ = _prepare_part(
        storage,
        renderer,
        relative_path=relative_path,
        expected_hash=expected_hash,
        part_index=part_index,
        dest_dir=dest_dir,
        filename=filename,
    )
    return plan


def _fsync_directory(path: Path) -> None:
    if sys.platform == "win32":
        return
    directory_fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_temporary(dest_dir: Path, payload: bytes) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".mail-dock-attachment-",
        suffix=".tmp",
        dir=dest_dir,
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


def _commit_without_overwrite(temporary_path: Path, destination: Path) -> Path:
    """Publish the temp file without allowing a racing writer to overwrite it."""

    try:
        os.link(temporary_path, destination)
    except FileExistsError:
        raise
    else:
        with suppress(OSError):
            temporary_path.unlink(missing_ok=True)
    return destination


def commit_attachment_save(
    storage: BaseEmlStorage,
    renderer: BaseMessageRenderer,
    *,
    plan: AttachmentSavePlan,
    overwrite: bool = False,
) -> SavedFile:
    """Revalidate a reviewed plan and atomically write its attachment bytes."""

    if not plan.dest_dir.is_dir():
        raise StorageError("Attachment destination directory is unavailable")

    raw = storage.read_verified(plan.relative_path, plan.expected_hash)
    try:
        part = _part_at(renderer.render(raw).parts, plan.part_index)
        sanitized = sanitize_attachment_name(plan.filename)
        if sanitized.is_executable != plan.is_executable:
            raise StorageError("Attachment security classification changed")
        if sanitized.name != plan.filename:
            raise StorageError("Attachment filename changed after confirmation")
        destination = resolve_within(plan.dest_dir, plan.filename)
    except ValueError as error:
        raise StorageError("Attachment destination is no longer safe") from error

    temporary_path: Path | None = None
    try:
        temporary_path = _write_temporary(plan.dest_dir, part.payload)
        temporary_path_for_commit = temporary_path
        if overwrite:
            os.replace(temporary_path_for_commit, destination)  # noqa: PTH105
            temporary_path = None
        else:
            while True:
                try:
                    _commit_without_overwrite(temporary_path_for_commit, destination)
                    temporary_path = None
                    break
                except FileExistsError:
                    next_name = _candidate_name(
                        f"{Path(plan.filename).stem} ({_next_counter(destination)})"
                        f"{Path(plan.filename).suffix}",
                        plan.dest_dir,
                    )
                    destination = resolve_within(plan.dest_dir, next_name)
        _fsync_directory(plan.dest_dir)
    except OSError as error:
        raise StorageError("Could not save attachment") from error
    finally:
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)

    return SavedFile(destination, plan.warnings, plan.is_executable)


def _next_counter(destination: Path) -> int:
    stem = destination.stem
    marker = " ("
    if marker not in stem or not stem.endswith(")"):
        return 1
    try:
        return int(stem.rsplit(marker, 1)[1][:-1]) + 1
    except ValueError:
        return 1
