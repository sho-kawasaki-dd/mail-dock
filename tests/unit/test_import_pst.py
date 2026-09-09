from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from mail_dock.domain.errors import (
    InsufficientSpaceError,
    OperationCancelledError,
    SourceChangedError,
)
from mail_dock.domain.fetcher import CancelToken
from mail_dock.usecases.import_pst import (
    ImportJobAction,
    ImportJobDecision,
    check_import_capacity,
    resolve_import_job,
    snapshot_source_file,
    validate_source_snapshot,
)
from tests.support.in_memory_repository import InMemoryPstImportRepository


def _record(
    import_uuid: str,
    source_sha256: str,
    *,
    status: str,
    active: int = 0,
) -> dict[str, object]:
    return {
        "import_uuid": import_uuid,
        "account_id": "pst-account",
        "source_filename": "archive.pst",
        "source_sha256": source_sha256,
        "status": status,
        "is_active": active,
    }


def test_snapshot_hashes_in_chunks_and_validate_detects_source_change(tmp_path: Path) -> None:
    source = tmp_path / "archive.pst"
    source.write_bytes(b"pst-content")

    snapshot = snapshot_source_file(source, chunk_size=3)
    assert snapshot.source_sha256 == hashlib.sha256(b"pst-content").hexdigest()
    assert snapshot.size_bytes == len(b"pst-content")
    assert snapshot.mtime_ns == source.stat().st_mtime_ns
    assert snapshot.file_identity is not None
    assert validate_source_snapshot(source, snapshot) == snapshot

    source.write_bytes(b"changed-pst-content")
    with pytest.raises(SourceChangedError, match="changed"):
        validate_source_snapshot(source, snapshot)


def test_snapshot_honors_cancellation(tmp_path: Path) -> None:
    source = tmp_path / "archive.pst"
    source.write_bytes(b"pst-content")
    token = CancelToken()
    token.cancel()

    with pytest.raises(OperationCancelledError):
        snapshot_source_file(source, cancel=token)


def test_resolve_incomplete_import_requires_choice_then_resumes_or_discards() -> None:
    repository = InMemoryPstImportRepository()
    digest = "a" * 64
    first_id = repository.create_import(_record("first", digest, status="cancelled_resumable"))
    repository.create_import(_record("second", digest, status="failed_resumable"))

    pending = resolve_import_job(repository, digest)
    assert pending.action is ImportJobAction.NEEDS_INCOMPLETE_DECISION
    assert [record["id"] for record in pending.incomplete_records] == [first_id, 2]

    resumed = resolve_import_job(repository, digest, decision=ImportJobDecision.RESUME)
    assert resumed.action is ImportJobAction.RESUME
    assert resumed.import_record is not None
    assert resumed.import_record["id"] == first_id

    discarded = resolve_import_job(repository, digest, decision="discard_incomplete")
    assert discarded.action is ImportJobAction.DISCARD_INCOMPLETE
    assert len(discarded.incomplete_records) == 2


def test_resolve_active_completed_import_requires_cancel_or_reimport() -> None:
    repository = InMemoryPstImportRepository()
    digest = "b" * 64
    active_id = repository.create_import(
        _record("complete", digest, status="completed", active=1)
    )

    pending = resolve_import_job(repository, digest)
    assert pending.action is ImportJobAction.NEEDS_REIMPORT_DECISION
    assert pending.replaces_id == active_id

    cancelled = resolve_import_job(repository, digest, decision="cancel")
    assert cancelled.action is ImportJobAction.CANCEL
    reimported = resolve_import_job(repository, digest, decision="reimport")
    assert reimported.action is ImportJobAction.REIMPORT
    assert reimported.replaces_id == active_id


def test_resolve_new_source_uses_complete_hash_only() -> None:
    repository = InMemoryPstImportRepository()
    assert resolve_import_job(repository, "C" * 64).action is ImportJobAction.NEW
    with pytest.raises(ValueError, match="complete SHA-256"):
        resolve_import_job(repository, "c" * 12)


def test_check_import_capacity_includes_retained_generation(tmp_path: Path) -> None:
    checks: list[Path] = []

    def check(path: Path) -> object:
        checks.append(path)
        return object()

    result = check_import_capacity(
        tmp_path,
        10,
        retained_bytes=4,
        check_free_space=check,
        free_space=lambda _path: 30,
    )
    assert checks == [tmp_path]
    assert result.required_bytes == 29
    assert result.available_bytes == 30

    with pytest.raises(InsufficientSpaceError, match="requires"):
        check_import_capacity(
            tmp_path,
            10,
            check_free_space=check,
            free_space=lambda _path: 24,
        )