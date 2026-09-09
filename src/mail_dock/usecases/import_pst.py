"""Preflight and job-resolution use cases for PST imports."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from mail_dock.domain.errors import (
    InsufficientSpaceError,
    SourceChangedError,
    UnreadableArchive,
)
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.repository import BasePstImportRepository, MessageRecord

_HASH_LENGTH = hashlib.sha256().digest_size * 2
_CHUNK_SIZE = 1024 * 1024
_COMPLETE_STATUSES = frozenset({"completed", "completed_with_errors"})
_INCOMPLETE_STATUSES = frozenset(
    {
        "extracting",
        "ready_to_ingest",
        "ingesting",
        "cancelled_resumable",
        "failed_resumable",
    }
)


@dataclass(frozen=True)
class SourceFileSnapshot:
    """Immutable identity captured for a PST before extraction begins."""

    source_sha256: str
    size_bytes: int
    mtime_ns: int
    file_identity: tuple[int, int] | None


class ImportJobDecision(StrEnum):
    """Actions available when an identical PST was previously imported."""

    RESUME = "resume"
    DISCARD_INCOMPLETE = "discard_incomplete"
    CANCEL = "cancel"
    REIMPORT = "reimport"


class ImportJobAction(StrEnum):
    """Resolution returned to the caller before Stage A starts."""

    NEW = "new"
    NEEDS_INCOMPLETE_DECISION = "needs_incomplete_decision"
    RESUME = "resume"
    DISCARD_INCOMPLETE = "discard_incomplete"
    NEEDS_REIMPORT_DECISION = "needs_reimport_decision"
    CANCEL = "cancel"
    REIMPORT = "reimport"


@dataclass(frozen=True)
class ImportJobResolution:
    """Deterministic result of resolving an existing source hash."""

    action: ImportJobAction
    import_record: MessageRecord | None = None
    incomplete_records: tuple[MessageRecord, ...] = ()
    replaces_id: int | None = None

    @property
    def requires_user_choice(self) -> bool:
        return self.action in {
            ImportJobAction.NEEDS_INCOMPLETE_DECISION,
            ImportJobAction.NEEDS_REIMPORT_DECISION,
        }


@dataclass(frozen=True)
class ImportCapacity:
    """The capacity calculation used to admit a PST import."""

    available_bytes: int
    required_bytes: int
    source_size_bytes: int
    retained_bytes: int


def _file_identity(metadata: os.stat_result) -> tuple[int, int] | None:
    device = getattr(metadata, "st_dev", None)
    inode = getattr(metadata, "st_ino", None)
    if not isinstance(device, int) or not isinstance(inode, int):
        return None
    return device, inode


def _snapshot_metadata(metadata: os.stat_result) -> tuple[int, int, tuple[int, int] | None]:
    return metadata.st_size, metadata.st_mtime_ns, _file_identity(metadata)


def snapshot_source_file(
    source: Path,
    *,
    cancel: CancelToken | None = None,
    chunk_size: int = _CHUNK_SIZE,
) -> SourceFileSnapshot:
    """Hash a PST through a read-only stream and capture its file metadata."""

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    token = cancel or CancelToken()
    token.raise_if_cancelled()
    digest = hashlib.sha256()
    try:
        with source.open("rb") as source_file:
            initial = os.fstat(source_file.fileno())
            if not stat.S_ISREG(initial.st_mode):
                raise UnreadableArchive(f"PST source is not a regular file: {source}")
            for chunk in iter(lambda: source_file.read(chunk_size), b""):
                token.raise_if_cancelled()
                digest.update(chunk)
            final = os.fstat(source_file.fileno())
    except UnreadableArchive:
        raise
    except OSError as error:
        raise UnreadableArchive(f"Could not read PST source: {source}") from error

    if _snapshot_metadata(initial) != _snapshot_metadata(final):
        raise SourceChangedError(f"PST source changed while it was being read: {source}")
    return SourceFileSnapshot(
        source_sha256=digest.hexdigest(),
        size_bytes=final.st_size,
        mtime_ns=final.st_mtime_ns,
        file_identity=_file_identity(final),
    )


def validate_source_snapshot(
    source: Path,
    expected: SourceFileSnapshot,
    *,
    cancel: CancelToken | None = None,
) -> SourceFileSnapshot:
    """Re-hash a source and reject any change since the recorded snapshot."""

    actual = snapshot_source_file(source, cancel=cancel)
    if actual != expected:
        raise SourceChangedError(f"PST source changed since it was inspected: {source}")
    return actual


def _validate_source_sha256(source_sha256: str) -> str:
    normalized = source_sha256.casefold()
    if len(normalized) != _HASH_LENGTH or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError("source_sha256 must be a complete SHA-256 digest")
    return normalized


def resolve_import_job(
    repository: BasePstImportRepository,
    source_sha256: str,
    *,
    decision: ImportJobDecision | str | None = None,
) -> ImportJobResolution:
    """Resolve resume/discard or cancel/re-import for a complete source hash.

    This function only returns a decision. Destructive abandonment and new-row
    creation belong to the later import workflow, after the UI has confirmed
    the returned choice.
    """

    source_sha256 = _validate_source_sha256(source_sha256)
    selected_decision = None if decision is None else ImportJobDecision(decision)
    incomplete = tuple(
        sorted(
            (
                record
                for record in repository.find_incomplete_by_source_sha256(source_sha256)
                if record.get("status") in _INCOMPLETE_STATUSES
            ),
            key=lambda record: int(record.get("id", 0)),
        )
    )
    if incomplete:
        if selected_decision is None:
            return ImportJobResolution(
                ImportJobAction.NEEDS_INCOMPLETE_DECISION,
                incomplete_records=incomplete,
            )
        if selected_decision is ImportJobDecision.RESUME:
            return ImportJobResolution(
                ImportJobAction.RESUME,
                import_record=incomplete[0],
                incomplete_records=incomplete,
            )
        if selected_decision is ImportJobDecision.DISCARD_INCOMPLETE:
            return ImportJobResolution(
                ImportJobAction.DISCARD_INCOMPLETE,
                incomplete_records=incomplete,
            )
        raise ValueError("An incomplete PST import accepts resume or discard_incomplete")

    active = repository.find_active_by_source_sha256(source_sha256)
    if active is not None and active.get("status") in _COMPLETE_STATUSES:
        replaces_id = int(active["id"]) if active.get("id") is not None else None
        if selected_decision is None:
            return ImportJobResolution(
                ImportJobAction.NEEDS_REIMPORT_DECISION,
                import_record=active,
                replaces_id=replaces_id,
            )
        if selected_decision is ImportJobDecision.CANCEL:
            return ImportJobResolution(
                ImportJobAction.CANCEL,
                import_record=active,
                replaces_id=replaces_id,
            )
        if selected_decision is ImportJobDecision.REIMPORT:
            return ImportJobResolution(
                ImportJobAction.REIMPORT,
                import_record=active,
                replaces_id=replaces_id,
            )
        raise ValueError("A completed PST import accepts cancel or reimport")

    if selected_decision is not None:
        raise ValueError("No existing PST import requires the selected decision")
    return ImportJobResolution(ImportJobAction.NEW)


def check_import_capacity(
    storage_root: Path,
    source_size_bytes: int,
    *,
    retained_bytes: int = 0,
    check_free_space: Callable[[Path], object],
    free_space: Callable[[Path], int],
) -> ImportCapacity:
    """Require the global storage policy and PST-size-based working space."""

    if source_size_bytes < 0 or retained_bytes < 0:
        raise ValueError("source_size_bytes and retained_bytes must be non-negative")
    check_free_space(storage_root)
    available_bytes = free_space(storage_root)
    required_bytes = (source_size_bytes * 5 + 1) // 2 + retained_bytes
    if available_bytes < required_bytes:
        raise InsufficientSpaceError(
            "PST import requires "
            f"{required_bytes} free bytes, but only {available_bytes} are available"
        )
    return ImportCapacity(
        available_bytes=available_bytes,
        required_bytes=required_bytes,
        source_size_bytes=source_size_bytes,
        retained_bytes=retained_bytes,
    )


__all__ = [
    "ImportCapacity",
    "ImportJobAction",
    "ImportJobDecision",
    "ImportJobResolution",
    "SourceFileSnapshot",
    "check_import_capacity",
    "resolve_import_job",
    "snapshot_source_file",
    "validate_source_snapshot",
]