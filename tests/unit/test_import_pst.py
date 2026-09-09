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
from mail_dock.infrastructure.storage.eml_storage import EmlStorage
from mail_dock.infrastructure.storage.pst_import_storage import PstImportStorage
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
    restore_generation,
    run_stage_a,
    run_stage_b,
    snapshot_source_file,
    switch_generation,
    validate_source_snapshot,
)
from tests.support.in_memory_repository import InMemoryPstImportRepository

PST_STORAGE = PstImportStorage()


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
        content = (
            b"Subject: test\nContent-Transfer-Encoding: base64\n\nnot-base64\n"
            if self.action == "malformed"
            else b"Subject: test\n\nbody\n"
        )
        (staging / "Store" / "Inbox" / "1.eml").write_bytes(content)
        if self.before_progress is not None:
            self.before_progress()
        on_progress(1)
        return ExtractResult(staging, 1, "", "")


class _WriteGate:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed

    def is_write_allowed(self) -> bool:
        return self.allowed


class _FailOnFlushManifest(PstManifestWriter):
    def __init__(self, root: Path, import_uuid: str, fail_on: int) -> None:
        super().__init__(root, import_uuid)
        self._flush_count = 0
        self._fail_on = fail_on

    def flush_and_sync(self) -> None:
        self._flush_count += 1
        if self._flush_count == self._fail_on:
            raise OSError("injected manifest fsync failure")
        super().flush_and_sync()


def _stage_a_import(tmp_path: Path) -> tuple[Path, InMemoryPstImportRepository, str, int]:
    source = tmp_path / "archive.pst"
    source.write_bytes(b"pst-content")
    import_uuid = "12345678-1234-4234-8234-123456789abc"
    repository = InMemoryPstImportRepository()
    import_id = repository.create_import(
        _record(import_uuid, hashlib.sha256(b"pst-content").hexdigest(), status="new")
    )
    return source, repository, import_uuid, import_id


def _complete_generation(
    tmp_path: Path,
    repository: InMemoryPstImportRepository,
    source: Path,
    import_uuid: str,
    account_id: str,
    *,
    active: int = 0,
    replaces_id: int | None = None,
) -> int:
    import_id = repository.create_import(
        _record(
            import_uuid,
            hashlib.sha256(source.read_bytes()).hexdigest(),
            status="new",
            active=active,
        )
        | {"account_id": account_id, "replaces_id": replaces_id}
    )
    with PstManifestWriter(tmp_path, import_uuid) as manifest:
        stage_a = run_stage_a(
            repository,
            manifest,
            _FakeExtractor(),
            import_id=import_id,
            import_uuid=import_uuid,
            account_id=account_id,
            source=source,
            storage_root=tmp_path,
            source_snapshot=snapshot_source_file(source, pst_storage=PST_STORAGE),
            readpst_version="0.6.76",
            options=ImportOptions("Archive", "cp932"),
            pst_storage=PST_STORAGE,
        )
    with PstManifestWriter(tmp_path, import_uuid) as manifest:
        run_stage_b(
            repository,
            manifest,
            EmlStorage(tmp_path),
            import_id=import_id,
            import_uuid=import_uuid,
            account_id=account_id,
            staging_root=stage_a.staging_root,
            pst_storage=PST_STORAGE,
        )
    repository.imports[import_id]["is_active"] = active
    return import_id


def test_run_stage_a_publishes_inventory_and_atomic_marker(tmp_path: Path) -> None:
    source, repository, import_uuid, import_id = _stage_a_import(tmp_path)
    snapshot = snapshot_source_file(source, pst_storage=PST_STORAGE, chunk_size=3)
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
            pst_storage=PST_STORAGE,
        )

    assert result.staging_root == tmp_path / "tmp" / "pstimp" / "12345678"
    assert Path(result.marker_path).is_file()
    marker = read_stage_a_marker(result.marker_path, PST_STORAGE)
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
                source_snapshot=snapshot_source_file(source, pst_storage=PST_STORAGE),
                readpst_version="0.6.76",
                options=ImportOptions("Archive", "cp932"),
                pst_storage=PST_STORAGE,
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
            source_snapshot=snapshot_source_file(source, pst_storage=PST_STORAGE),
            readpst_version="0.6.76",
            options=ImportOptions("Archive", "cp932"),
            pst_storage=PST_STORAGE,
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
                source_snapshot=snapshot_source_file(source, pst_storage=PST_STORAGE),
                readpst_version="0.6.76",
                options=ImportOptions("Archive", "cp932"),
                storage_state=gate,
                on_progress=lambda _count: None,
                pst_storage=PST_STORAGE,
            )

    stage_root = tmp_path / "tmp" / "pstimp" / "12345678"
    assert stage_root.exists()
    assert repository.imports[import_id]["status"] == "extracting"


def test_run_stage_b_saves_messages_in_batches_and_removes_staging(tmp_path: Path) -> None:
    source, repository, import_uuid, import_id = _stage_a_import(tmp_path)
    with PstManifestWriter(tmp_path, import_uuid) as manifest:
        stage_a = run_stage_a(
            repository,
            manifest,
            _FakeExtractor(),
            import_id=import_id,
            import_uuid=import_uuid,
            account_id="pst-account",
            source=source,
            storage_root=tmp_path,
            source_snapshot=snapshot_source_file(source, pst_storage=PST_STORAGE),
            readpst_version="0.6.76",
            options=ImportOptions("Archive", "cp932"),
            pst_storage=PST_STORAGE,
        )

    with PstManifestWriter(tmp_path, import_uuid) as manifest:
        result = run_stage_b(
            repository,
            manifest,
            EmlStorage(tmp_path),
            import_id=import_id,
            import_uuid=import_uuid,
            account_id="pst-account",
            staging_root=stage_a.staging_root,
            batch_size=1,
            pst_storage=PST_STORAGE,
        )

    assert result.status == "completed"
    assert result.ingested_count == 1
    assert not Path(stage_a.staging_root).exists()
    item = repository.items[(import_id, "Store/Inbox/1.eml")]
    assert item["status"] == "saved"
    assert repository.messages[1]["remote_state"] == "no_remote"
    assert repository.messages[1]["date_sent"] is None
    events = list(PstManifestReader(tmp_path, import_uuid).read_all_events())
    assert [event["event"] for event in events][-1] == "item_saved"


def test_run_stage_b_reuses_durable_saved_event_on_resume(tmp_path: Path) -> None:
    source, repository, import_uuid, import_id = _stage_a_import(tmp_path)
    with PstManifestWriter(tmp_path, import_uuid) as manifest:
        stage_a = run_stage_a(
            repository,
            manifest,
            _FakeExtractor(),
            import_id=import_id,
            import_uuid=import_uuid,
            account_id="pst-account",
            source=source,
            storage_root=tmp_path,
            source_snapshot=snapshot_source_file(source, pst_storage=PST_STORAGE),
            readpst_version="0.6.76",
            options=ImportOptions("Archive", "cp932"),
            pst_storage=PST_STORAGE,
        )
        stored = EmlStorage(tmp_path).save_from_file(
            "pst-account", None, Path(stage_a.staging_root) / "Store/Inbox/1.eml"
        )
        manifest.append(
            {
                "event": "item_saved",
                "import_uuid": import_uuid,
                "timestamp": "2025-01-01T00:00:00+00:00",
                "source_item_key": "Store/Inbox/1.eml",
                "final_relative_path": stored.relative_path,
                "file_hash": stored.file_hash,
                "size_bytes": stored.size_bytes,
            }
        )
        manifest.flush_and_sync()

    with PstManifestWriter(tmp_path, import_uuid) as manifest:
        result = run_stage_b(
            repository,
            manifest,
            EmlStorage(tmp_path),
            import_id=import_id,
            import_uuid=import_uuid,
            account_id="pst-account",
            staging_root=stage_a.staging_root,
            pst_storage=PST_STORAGE,
        )

    assert result.status == "completed"
    saved_events = [
        event
        for event in PstManifestReader(tmp_path, import_uuid).read_all_events()
        if event["event"] == "item_saved"
    ]
    assert len(saved_events) == 1


def test_run_stage_b_cancellation_keeps_resumable_staging(tmp_path: Path) -> None:
    source, repository, import_uuid, import_id = _stage_a_import(tmp_path)
    with PstManifestWriter(tmp_path, import_uuid) as manifest:
        stage_a = run_stage_a(
            repository,
            manifest,
            _FakeExtractor(),
            import_id=import_id,
            import_uuid=import_uuid,
            account_id="pst-account",
            source=source,
            storage_root=tmp_path,
            source_snapshot=snapshot_source_file(source, pst_storage=PST_STORAGE),
            readpst_version="0.6.76",
            options=ImportOptions("Archive", "cp932"),
            pst_storage=PST_STORAGE,
        )

    token = CancelToken()
    token.cancel()
    with PstManifestWriter(tmp_path, import_uuid) as manifest, pytest.raises(
        OperationCancelledError
    ):
        run_stage_b(
            repository,
            manifest,
            EmlStorage(tmp_path),
            import_id=import_id,
            import_uuid=import_uuid,
            account_id="pst-account",
            staging_root=stage_a.staging_root,
            cancel=token,
            pst_storage=PST_STORAGE,
        )

    assert repository.imports[import_id]["status"] == "cancelled_resumable"
    assert Path(stage_a.staging_root).exists()


def test_run_stage_b_keeps_parse_failures_as_completed_with_errors(tmp_path: Path) -> None:
    source, repository, import_uuid, import_id = _stage_a_import(tmp_path)
    with PstManifestWriter(tmp_path, import_uuid) as manifest:
        stage_a = run_stage_a(
            repository,
            manifest,
            _FakeExtractor("malformed"),
            import_id=import_id,
            import_uuid=import_uuid,
            account_id="pst-account",
            source=source,
            storage_root=tmp_path,
            source_snapshot=snapshot_source_file(source, pst_storage=PST_STORAGE),
            readpst_version="0.6.76",
            options=ImportOptions("Archive", "cp932"),
            pst_storage=PST_STORAGE,
        )

    with PstManifestWriter(tmp_path, import_uuid) as manifest:
        result = run_stage_b(
            repository,
            manifest,
            EmlStorage(tmp_path),
            import_id=import_id,
            import_uuid=import_uuid,
            account_id="pst-account",
            staging_root=stage_a.staging_root,
            pst_storage=PST_STORAGE,
        )

    assert result.status == "completed_with_errors"
    assert result.failed_count == 1
    assert repository.items[(import_id, "Store/Inbox/1.eml")]["error_class"] == "parse"
    assert not Path(stage_a.staging_root).exists()
    assert any(
        event["event"] == "item_parse_failed"
        for event in PstManifestReader(tmp_path, import_uuid).read_all_events()
    )


def test_switch_generation_verifies_durable_state_and_records_lifecycle(
    tmp_path: Path,
) -> None:
    source = tmp_path / "archive.pst"
    source.write_bytes(b"pst-content")
    repository = InMemoryPstImportRepository()
    old_uuid = "11111111-1111-4111-8111-111111111111"
    new_uuid = "22222222-2222-4222-8222-222222222222"
    old_id = _complete_generation(tmp_path, repository, source, old_uuid, "old-account", active=1)
    new_id = _complete_generation(
        tmp_path,
        repository,
        source,
        new_uuid,
        "new-account",
        replaces_id=old_id,
    )

    with PstManifestWriter(tmp_path, new_uuid) as manifest:
        verification = switch_generation(
            repository,
            manifest,
            EmlStorage(tmp_path),
            import_id=new_id,
            import_uuid=new_uuid,
            replaces_id=old_id,
            replaces_import_uuid=old_uuid,
        )

    assert verification.item_count == 1
    assert repository.imports[new_id]["is_active"] == 1
    assert repository.imports[old_id]["status"] == "superseded"
    old_message_id = next(
        item["message_row_id"]
        for (item_import_id, _), item in repository.items.items()
        if item_import_id == old_id
    )
    assert repository.messages[old_message_id]["local_state"] == "trashed"
    assert [
        event["event"]
        for event in PstManifestReader(tmp_path, new_uuid).read_all_events()
        if str(event["event"]).startswith("generation_")
    ] == ["generation_switch_prepared", "generation_switch_committed", "generation_superseded"]


def test_switch_generation_rolls_back_when_committed_event_cannot_sync(
    tmp_path: Path,
) -> None:
    source = tmp_path / "archive.pst"
    source.write_bytes(b"pst-content")
    repository = InMemoryPstImportRepository()
    old_uuid = "33333333-3333-4333-8333-333333333333"
    new_uuid = "44444444-4444-4444-8444-444444444444"
    old_id = _complete_generation(tmp_path, repository, source, old_uuid, "old-account", active=1)
    new_id = _complete_generation(
        tmp_path,
        repository,
        source,
        new_uuid,
        "new-account",
        replaces_id=old_id,
    )

    manifest = _FailOnFlushManifest(tmp_path, new_uuid, fail_on=2)
    with pytest.raises(OSError, match="fsync"):
        switch_generation(
            repository,
            manifest,
            EmlStorage(tmp_path),
            import_id=new_id,
            import_uuid=new_uuid,
            replaces_id=old_id,
            replaces_import_uuid=old_uuid,
        )
    manifest.close()

    assert repository.imports[old_id]["is_active"] == 1
    assert repository.imports[old_id]["status"] == "completed"
    assert repository.imports[new_id]["is_active"] == 0
    assert repository.imports[new_id]["status"] == "completed"


def test_restore_generation_reverses_visibility_and_records_event(tmp_path: Path) -> None:
    source = tmp_path / "archive.pst"
    source.write_bytes(b"pst-content")
    repository = InMemoryPstImportRepository()
    old_uuid = "55555555-5555-4555-8555-555555555555"
    new_uuid = "66666666-6666-4666-8666-666666666666"
    old_id = _complete_generation(tmp_path, repository, source, old_uuid, "old-account", active=1)
    new_id = _complete_generation(
        tmp_path,
        repository,
        source,
        new_uuid,
        "new-account",
        replaces_id=old_id,
    )
    with PstManifestWriter(tmp_path, new_uuid) as manifest:
        switch_generation(
            repository,
            manifest,
            EmlStorage(tmp_path),
            import_id=new_id,
            import_uuid=new_uuid,
            replaces_id=old_id,
            replaces_import_uuid=old_uuid,
        )

    with PstManifestWriter(tmp_path, old_uuid) as manifest:
        restore_generation(
            repository,
            manifest,
            EmlStorage(tmp_path),
            import_id=old_id,
            import_uuid=old_uuid,
            superseded_import_uuid=new_uuid,
        )

    assert repository.imports[old_id]["is_active"] == 1
    assert repository.imports[old_id]["status"] == "completed"
    assert repository.imports[new_id]["is_active"] == 0
    assert repository.imports[new_id]["status"] == "superseded"
    old_message_id = next(
        item["message_row_id"]
        for (item_import_id, _), item in repository.items.items()
        if item_import_id == old_id
    )
    new_message_id = next(
        item["message_row_id"]
        for (item_import_id, _), item in repository.items.items()
        if item_import_id == new_id
    )
    assert repository.messages[old_message_id]["local_state"] == "active"
    assert repository.messages[new_message_id]["local_state"] == "trashed"
    assert any(
        event["event"] == "generation_restored"
        for event in PstManifestReader(tmp_path, old_uuid).read_all_events()
    )


def test_snapshot_hashes_in_chunks_and_validate_detects_source_change(tmp_path: Path) -> None:
    source = tmp_path / "archive.pst"
    source.write_bytes(b"pst-content")

    snapshot = snapshot_source_file(source, pst_storage=PST_STORAGE)
    assert snapshot.source_sha256 == hashlib.sha256(b"pst-content").hexdigest()
    assert snapshot.size_bytes == len(b"pst-content")
    assert snapshot.mtime_ns == source.stat().st_mtime_ns
    assert snapshot.file_identity is not None
    assert validate_source_snapshot(source, snapshot, pst_storage=PST_STORAGE) == snapshot

    source.write_bytes(b"changed-pst-content")
    with pytest.raises(SourceChangedError, match="changed"):
        validate_source_snapshot(source, snapshot, pst_storage=PST_STORAGE)


def test_snapshot_honors_cancellation(tmp_path: Path) -> None:
    source = tmp_path / "archive.pst"
    source.write_bytes(b"pst-content")
    token = CancelToken()
    token.cancel()

    with pytest.raises(OperationCancelledError):
        snapshot_source_file(source, pst_storage=PST_STORAGE, cancel=token)


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