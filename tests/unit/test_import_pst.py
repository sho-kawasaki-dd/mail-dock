from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

import pytest

from mail_dock.domain.errors import (
    ConverterFailed,
    InsufficientSpaceError,
    OperationCancelledError,
    SourceChangedError,
    StorageDetachedError,
)
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.importer import ArchiveInfo, BaseArchiveImporter, ExtractResult, ImportOptions
from mail_dock.infrastructure.storage.pst_manifest import (
    PstManifestReader,
    PstManifestWriter,
)
from mail_dock.usecases.import_pst import (
    ImportJobAction,
    ImportJobDecision,
    check_import_capacity,
    read_stage_a_marker,
    resolve_import_job,
    run_stage_a,
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


class _FakeExtractor(BaseArchiveImporter):
    def __init__(
        self, action: str = "success", before_progress: Callable[[], None] | None = None
    ) -> None:
        self.action = action
        self.before_progress = before_progress

    def probe(
        self,
        source: Path,
        *,
        cancel: CancelToken,
        on_progress: Callable[[int], None],
    ) -> ArchiveInfo:
        raise NotImplementedError

    def extract(
        self,
        source: Path,
        staging: Path,
        options: ImportOptions,
        *,
        cancel: CancelToken,
        on_progress: Callable[[int], None],
    ) -> ExtractResult:
        del source, options
        if self.action == "failed":
            raise ConverterFailed("fake readpst failure")
        if self.action == "cancelled":
            raise OperationCancelledError("fake extraction cancelled")
        (staging / "Store" / "Inbox").mkdir(parents=True)
        (staging / "Store" / "Inbox" / "1.eml").write_bytes(b"Subject: test\n\nbody\n")
        if self.before_progress is not None:
            self.before_progress()
        on_progress(1)
        return ExtractResult(staging, 1, "", "")


class _WriteGate:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed

    def is_write_allowed(self) -> bool:
        return self.allowed


def _stage_a_import(tmp_path: Path) -> tuple[Path, InMemoryPstImportRepository, str, int]:
    source = tmp_path / "archive.pst"
    source.write_bytes(b"pst-content")
    import_uuid = "12345678-1234-4234-8234-123456789abc"
    repository = InMemoryPstImportRepository()
    import_id = repository.create_import(
        _record(import_uuid, hashlib.sha256(b"pst-content").hexdigest(), status="new")
    )
    return source, repository, import_uuid, import_id


def test_run_stage_a_publishes_inventory_and_atomic_marker(tmp_path: Path) -> None:
    source, repository, import_uuid, import_id = _stage_a_import(tmp_path)
    snapshot = snapshot_source_file(source)
    options = ImportOptions("Archive", "cp932")
    with PstManifestWriter(tmp_path, import_uuid) as manifest:
        result = run_stage_a(
            repository,
            manifest,
            _FakeExtractor(),
            import_id=import_id,
            import_uuid=import_uuid,
            account_id="pst_account",
            source=source,
            storage_root=tmp_path,
            source_snapshot=snapshot,
            readpst_version="0.6.76",
            options=options,
        )

    assert result.staging_root == tmp_path / "tmp" / "pstimp" / "12345678"
    assert result.marker_path.is_file()
    marker = read_stage_a_marker(result.marker_path)
    assert marker["total_files"] == 1
    assert marker["inventory_sha256"] == result.inventory_sha256
    assert repository.imports[import_id]["status"] == "ready_to_ingest"
    assert repository.items[(import_id, "Store/Inbox/1.eml")]["status"] == "discovered"
    reader = PstManifestReader(tmp_path, import_uuid)
    assert reader.read_folders_manifest() == [
        {"raw_name": "Store", "display_name": "Store"},
        {"raw_name": "Store/Inbox", "display_name": "Inbox"},
    ]
    assert [event["event"] for event in reader.read_all_events()] == [
        "item_discovered",
        "import_ready",
    ]


def test_run_stage_a_failure_marks_abandoned_and_discards_staging(tmp_path: Path) -> None:
    source, repository, import_uuid, import_id = _stage_a_import(tmp_path)
    with PstManifestWriter(tmp_path, import_uuid) as manifest, pytest.raises(
        ConverterFailed, match="fake readpst"
    ):
            run_stage_a(
                repository,
                manifest,
                _FakeExtractor("failed"),
                import_id=import_id,
                import_uuid=import_uuid,
                account_id="pst_account",
                source=source,
                storage_root=tmp_path,
                source_snapshot=snapshot_source_file(source),
                readpst_version="0.6.76",
                options=ImportOptions("Archive", "cp932"),
            )

    stage_root = tmp_path / "tmp" / "pstimp" / "12345678"
    assert not stage_root.exists()
    assert repository.imports[import_id]["status"] == "abandoned"
    events = list(PstManifestReader(tmp_path, import_uuid).read_all_events())
    assert events[-1]["event"] == "import_abandoned"


def test_run_stage_a_cancellation_marks_abandoned_and_discards_staging(tmp_path: Path) -> None:
    source, repository, import_uuid, import_id = _stage_a_import(tmp_path)
    with PstManifestWriter(tmp_path, import_uuid) as manifest, pytest.raises(
        OperationCancelledError, match="fake extraction cancelled"
    ):
        run_stage_a(
            repository,
            manifest,
            _FakeExtractor("cancelled"),
            import_id=import_id,
            import_uuid=import_uuid,
            account_id="pst_account",
            source=source,
            storage_root=tmp_path,
            source_snapshot=snapshot_source_file(source),
            readpst_version="0.6.76",
            options=ImportOptions("Archive", "cp932"),
        )

    assert not (tmp_path / "tmp" / "pstimp" / "12345678").exists()
    assert repository.imports[import_id]["status"] == "abandoned"


def test_run_stage_a_does_not_write_or_delete_after_detach(tmp_path: Path) -> None:
    source, repository, import_uuid, import_id = _stage_a_import(tmp_path)
    gate = _WriteGate()
    with PstManifestWriter(tmp_path, import_uuid) as manifest, pytest.raises(
        StorageDetachedError
    ):
            run_stage_a(
                repository,
                manifest,
                _FakeExtractor(before_progress=lambda: setattr(gate, "allowed", False)),
                import_id=import_id,
                import_uuid=import_uuid,
                account_id="pst_account",
                source=source,
                storage_root=tmp_path,
                source_snapshot=snapshot_source_file(source),
                readpst_version="0.6.76",
                options=ImportOptions("Archive", "cp932"),
                storage_state=gate,
                on_progress=lambda _count: None,
            )

    stage_root = tmp_path / "tmp" / "pstimp" / "12345678"
    assert stage_root.exists()
    assert repository.imports[import_id]["status"] == "extracting"


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