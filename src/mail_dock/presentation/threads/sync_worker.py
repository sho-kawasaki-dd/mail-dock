"""Asynchronous synchronization and file-save worker for the GUI."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from PySide6.QtCore import Signal

from mail_dock.domain.errors import (
    AuthenticationError,
    DatabaseError,
    FetchError,
    MailDockError,
    OperationCancelledError,
    StorageDetachedError,
)
from mail_dock.domain.fetcher import BaseMailFetcher, CancelToken
from mail_dock.domain.messages import AttachmentSavePlan, SavedFile
from mail_dock.domain.ports import (
    BaseEmlStorage,
    BaseManifestWriter,
    BaseMessageRenderer,
    BasePstImportStorage,
    BasePstManifestWriter,
    BasePurgeStorage,
)
from mail_dock.domain.repository import (
    BaseMessageRepository,
    BasePstImportRepository,
    MessageRecord,
)
from mail_dock.presentation.errors import user_message
from mail_dock.presentation.threads.worker import OperationGate, Worker, _Task, operation_gate
from mail_dock.usecases.account_guards import is_pst_account
from mail_dock.usecases.delete_remote import (
    DeleteDryRunResult,
    DeleteResult,
    dry_run,
    execute,
)
from mail_dock.usecases.export_attachments import (
    ExportAttachmentsProgress,
    ExportAttachmentsResult,
    export_attachments,
)
from mail_dock.usecases.export_mbox import ExportMboxProgress, export_mbox
from mail_dock.usecases.export_message import export_eml
from mail_dock.usecases.import_pst import (
    ImportJobAction,
    ImportJobDecision,
    StageBResult,
    check_import_capacity,
    resolve_import_job,
    run_stage_a,
    run_stage_b,
    snapshot_source_file,
    switch_generation,
)
from mail_dock.usecases.reparse import ReparseResult, reparse_messages
from mail_dock.usecases.save_attachment import (
    commit_attachment_save,
    prepare_attachment_save,
)
from mail_dock.usecases.sync_folders import FolderRefreshResult, refresh_folders
from mail_dock.usecases.sync_mail import (
    SyncOptions,
    SyncProgress,
    SyncResult,
    force_fetch_message,
    sync_account,
)
from mail_dock.usecases.trash import (
    PurgeResult,
    TrashResult,
    purge,
    restore_from_trash,
)

SyncOperation = Literal[
    "sync",
    "refresh_folders",
    "prepare_attachment",
    "save_attachment",
    "export_eml",
    "export_mbox",
    "export_attachments",
    "restore_from_trash",
    "purge",
    "remote_delete_dry_run",
    "remote_delete",
    "failures_for_review",
    "audit_log",
    "force_fetch",
    "reparse",
    "pst_import",
    "pst_import_decision",
]
RepositoryFactory = Callable[[], BaseMessageRepository]
FetcherFactory = Callable[[MessageRecord], BaseMailFetcher]
StorageFactory = Callable[[], BaseEmlStorage]
RendererFactory = Callable[[], BaseMessageRenderer]
ManifestFactory = Callable[[str], BaseManifestWriter]
SyncAccountUseCase = Callable[..., SyncResult]
RefreshFoldersUseCase = Callable[..., FolderRefreshResult]
Clock = Callable[[], float]
PstImportRepositoryFactory = Callable[[], BasePstImportRepository]
PstImportStorageFactory = Callable[[], BasePstImportStorage]
PstImporterFactory = Callable[[], Any]


@dataclass(frozen=True)
class PstImportProgress:
    """Progress emitted while Stage A or Stage B processes staged files."""

    stage: str
    processed_count: int
    total_count: int | None = None


@dataclass(frozen=True)
class PstImportResult:
    """Presentation-safe summary of a completed PST generation import."""

    status: str
    import_id: int
    imported_count: int
    failed_count: int
    skipped_count: int = 0
    reimported: bool = False


@dataclass(frozen=True)
class PstImportDecisionRequest:
    """Existing-job choices that must be confirmed by the wizard."""

    source_sha256: str
    has_incomplete: bool
    has_completed: bool


@dataclass(frozen=True)
class SyncErrorNotification:
    """A worker failure with presentation-safe user-facing text."""

    operation: SyncOperation
    error: MailDockError
    message: str


@dataclass(frozen=True)
class FolderTreeSnapshot:
    """Repository-shaped account and folder data for the folder tree."""

    accounts: tuple[MessageRecord, ...]
    folders: tuple[MessageRecord, ...]
    pst_imports: tuple[MessageRecord, ...] = ()


@dataclass(frozen=True)
class _FolderRefreshTaskResult:
    result: FolderRefreshResult
    snapshot: FolderTreeSnapshot


@dataclass(frozen=True)
class _SyncTaskResult:
    operation: SyncOperation
    value: (
        SyncResult
        | _FolderRefreshTaskResult
        | FolderTreeSnapshot
        | AttachmentSavePlan
        | SavedFile
        | Path
        | ExportAttachmentsResult
        | TrashResult
        | PurgeResult
        | DeleteDryRunResult
        | DeleteResult
        | tuple[MessageRecord, ...]
        | ReparseResult
        | PstImportResult
        | PstImportDecisionRequest
    )


class SyncWorker(Worker):
    """Run synchronization and user-file writes on one dedicated QThread."""

    sync_progress = Signal(object)
    progress = Signal(object)
    sync_result = Signal(object)
    folders_refreshed = Signal(object)
    folder_tree_updated = Signal(object)
    error_reported = Signal(object)
    authentication_failed = Signal(object)
    fetch_failed = Signal(object)
    storage_detached = Signal(object)
    file_result = Signal(object)
    trash_result = Signal(object)
    purge_result = Signal(object)
    delete_dry_run_result = Signal(object)
    remote_delete_result = Signal(object)
    failures_for_review_result = Signal(object)
    failure_action_result = Signal(object)
    audit_log_result = Signal(object)
    pst_import_result = Signal(object)
    pst_import_decision_required = Signal(object)
    pst_import_cancelled = Signal(object)

    def __init__(
        self,
        repository: RepositoryFactory | BaseMessageRepository,
        fetcher_factory: FetcherFactory,
        storage_factory: StorageFactory,
        manifest_factory: ManifestFactory,
        *,
        renderer_factory: RendererFactory | None = None,
        sync_account_usecase: SyncAccountUseCase = sync_account,
        refresh_folders_usecase: RefreshFoldersUseCase = refresh_folders,
        sync_options: SyncOptions | None = None,
        connection_manager: Any | None = None,
        operation_gate: OperationGate | None = None,
        pst_import_repository: Callable[[], BasePstImportRepository] | None = None,
        pst_import_storage: PstImportStorageFactory | None = None,
        pst_importer: PstImporterFactory | None = None,
        pst_manifest_factory: Callable[[str], BasePstManifestWriter] | None = None,
        clock: Clock = time.monotonic,
    ) -> None:
        super().__init__(connection_manager)
        self._repository_factory = _as_repository_factory(repository)
        self._fetcher_factory = fetcher_factory
        self._storage_factory = storage_factory
        self._renderer_factory = renderer_factory
        self._manifest_factory = manifest_factory
        self._sync_account_usecase = sync_account_usecase
        self._refresh_folders_usecase = refresh_folders_usecase
        self._sync_options = sync_options or SyncOptions()
        self._clock = clock
        self._operation_gate = operation_gate
        self._pst_import_repository_factory = pst_import_repository
        self._pst_import_storage_factory = pst_import_storage
        self._pst_importer_factory = pst_importer
        self._pst_manifest_factory = pst_manifest_factory
        self._operations_by_token: dict[CancelToken, SyncOperation] = {}

        self.task_failed.connect(self._on_task_failed)
        self.task_completed.connect(self._on_task_completed)

    def sync_account(
        self,
        account_id: str,
        *,
        options: SyncOptions | None = None,
    ) -> CancelToken:
        """Queue synchronization for ``account_id`` and return its token."""

        selected_options = options or self._sync_options

        def operation(token: CancelToken) -> _SyncTaskResult:
            return _SyncTaskResult(
                "sync",
                self._run_sync_account(account_id, token, selected_options),
            )

        return self._submit_operation("sync", operation)

    request_sync_account = sync_account

    def sync_all_accounts(self, *, options: SyncOptions | None = None) -> CancelToken:
        """Queue one synchronization run for every enabled account."""

        selected_options = options or self._sync_options

        def operation(token: CancelToken) -> _SyncTaskResult:
            repository = self._repository_factory()
            account_ids = [
                account_id
                for account in repository.list_accounts()
                if _is_enabled_account(account)
                and not is_pst_account(account)
                and (account_id := _account_id(account))
            ]
            aggregate = SyncResult(0, 0, 0, 0, False)
            for account_id in account_ids:
                token.raise_if_cancelled()
                result = self._run_sync_account(account_id, token, selected_options)
                aggregate = _add_sync_results(aggregate, result)
                if result.cancelled:
                    break
            return _SyncTaskResult("sync", aggregate)

        return self._submit_operation("sync", operation)

    request_sync_all_accounts = sync_all_accounts

    def set_sync_options(self, options: SyncOptions) -> None:
        """Update defaults used by synchronization requests created later."""

        self._sync_options = options

    def _run_sync_account(
        self,
        account_id: str,
        token: CancelToken,
        options: SyncOptions,
    ) -> SyncResult:
        repository = self._repository_factory()
        account = _find_account(repository, account_id)
        fetcher = self._fetcher_factory(account)
        storage = self._storage_factory()
        manifest = self._manifest_factory(account_id)
        try:
            with fetcher:
                return self._sync_account_usecase(
                    fetcher,
                    repository,
                    storage,
                    manifest,
                    account_id=account_id,
                    options=options,
                    cancel=token,
                    on_progress=self._forward_progress(),
                )
        finally:
            _close_manifest(manifest)

    def refresh_folders(self, account_id: str) -> CancelToken:
        """Queue a remote folder refresh for ``account_id``."""

        def operation(token: CancelToken) -> _SyncTaskResult:
            repository = self._repository_factory()
            account = _find_account(repository, account_id)
            fetcher = self._fetcher_factory(account)
            with fetcher:
                token.raise_if_cancelled()
                result = self._refresh_folders_usecase(fetcher, repository, account_id)
            token.raise_if_cancelled()
            return _SyncTaskResult(
                "refresh_folders",
                _FolderRefreshTaskResult(
                    result,
                    _folder_tree_snapshot(repository, self._pst_import_repository_factory),
                ),
            )

        return self._submit_operation("refresh_folders", operation)

    request_refresh_folders = refresh_folders

    def list_failures_for_review(
        self, account_id: str | None = None, minimum_attempt_count: int = 10
    ) -> CancelToken:
        """Load failures requiring manual review on the database worker."""

        def operation(_token: CancelToken) -> _SyncTaskResult:
            failures = tuple(
                self._repository_factory().list_failures_for_review(
                    account_id, minimum_attempt_count
                )
            )
            return _SyncTaskResult("failures_for_review", failures)

        return self._submit_operation("failures_for_review", operation)

    def list_audit_log(self, limit: int, offset: int) -> CancelToken:
        """Load one page of the read-only audit log on the database worker."""

        def operation(_token: CancelToken) -> _SyncTaskResult:
            entries = tuple(self._repository_factory().list_audit_log(limit, offset))
            return _SyncTaskResult("audit_log", entries)

        return self._submit_operation("audit_log", operation)

    def force_fetch_message(self, failure: MessageRecord) -> CancelToken:
        """Fetch one reviewed oversized message without the configured limit."""

        def operation(token: CancelToken) -> _SyncTaskResult:
            account_id = _required_string(failure, "account_id")
            folder_raw_name = _required_string(failure, "folder_raw_name")
            folder_id = failure.get("folder_id")
            uidvalidity = _required_int(failure, "uidvalidity")
            uid = _required_int(failure, "uid")
            repository = self._repository_factory()
            fetcher = self._fetcher_factory(_find_account(repository, account_id))
            manifest = self._manifest_factory(account_id)
            try:
                with fetcher:
                    result = force_fetch_message(
                        fetcher,
                        repository,
                        self._storage_factory(),
                        manifest,
                        account_id=account_id,
                        folder_raw_name=folder_raw_name,
                        folder_id=folder_id,
                        uidvalidity=uidvalidity,
                        uid=uid,
                        cancel=token,
                    )
            finally:
                _close_manifest(manifest)
            return _SyncTaskResult("force_fetch", result)

        return self._submit_operation("force_fetch", operation)

    def reparse_messages(
        self, *, account_id: str | None = None, message_ids: tuple[int, ...] | None = None
    ) -> CancelToken:
        """Reparse reviewed parse failures on the database worker."""

        def operation(token: CancelToken) -> _SyncTaskResult:
            return _SyncTaskResult(
                "reparse",
                reparse_messages(
                    self._repository_factory(),
                    self._storage_factory(),
                    account_id=account_id,
                    only_failed=True,
                    message_ids=message_ids,
                    cancel=token,
                ),
            )

        return self._submit_operation("reparse", operation)

    def load_folder_tree(self) -> CancelToken:
        """Load the current account and folder tree on the sync worker."""

        def operation(_token: CancelToken) -> _SyncTaskResult:
            return _SyncTaskResult(
                "refresh_folders",
                _folder_tree_snapshot(
                    self._repository_factory(), self._pst_import_repository_factory
                ),
            )

        return self._submit_operation("refresh_folders", operation)

    def import_pst(
        self,
        source: Path,
        *,
        display_name: str,
        charset: str,
        include_deleted: bool,
        decision: ImportJobDecision | str | None = None,
        retained_bytes: int = 0,
        free_space: Callable[[Path], int] | None = None,
        check_free_space: Callable[[Path], object] | None = None,
    ) -> CancelToken:
        """Queue the complete PST import lifecycle on the single writer."""

        def operation(token: CancelToken) -> _SyncTaskResult:
            if self._pst_import_repository_factory is None:
                raise RuntimeError("PST import repository is not configured")
            if (
                self._pst_import_storage_factory is None
                or self._pst_importer_factory is None
                or self._pst_manifest_factory is None
            ):
                raise RuntimeError("PST import infrastructure is not configured")
            repository = self._pst_import_repository_factory()
            pst_storage = self._pst_import_storage_factory()
            importer = self._pst_importer_factory()
            source_snapshot = snapshot_source_file(source, pst_storage=pst_storage, cancel=token)
            if free_space is not None and check_free_space is not None:
                check_import_capacity(
                    cast(Any, self._storage_factory()).root,
                    source_snapshot.size_bytes,
                    retained_bytes=retained_bytes,
                    check_free_space=check_free_space,
                    free_space=free_space,
                )
            resolution = resolve_import_job(
                repository,
                source_snapshot.source_sha256,
                decision=decision,
            )
            if resolution.action is ImportJobAction.CANCEL:
                raise OperationCancelledError("PST import was cancelled")
            if resolution.action in {
                ImportJobAction.NEEDS_INCOMPLETE_DECISION,
                ImportJobAction.NEEDS_REIMPORT_DECISION,
            }:
                return _SyncTaskResult(
                    "pst_import_decision",
                    PstImportDecisionRequest(
                        source_snapshot.source_sha256,
                        bool(resolution.incomplete_records),
                        resolution.import_record is not None,
                    ),
                )

            if resolution.action is ImportJobAction.DISCARD_INCOMPLETE:
                storage_root = cast(Any, self._storage_factory()).root
                for incomplete in resolution.incomplete_records:
                    incomplete_id = incomplete.get("id")
                    incomplete_uuid = incomplete.get("import_uuid")
                    if not isinstance(incomplete_id, int) or not isinstance(incomplete_uuid, str):
                        continue
                    abandoned_manifest = self._pst_manifest_factory(incomplete_uuid)
                    try:
                        abandoned_manifest.append(
                            {
                                "event": "import_abandoned",
                                "import_uuid": incomplete_uuid,
                                "timestamp": datetime.now(UTC).isoformat(),
                                "reason": "user_discarded_incomplete_import",
                            }
                        )
                        abandoned_manifest.flush_and_sync()
                    finally:
                        _close_manifest(abandoned_manifest)
                    repository.update_import_status(
                        incomplete_id,
                        "abandoned",
                        staging_path=None,
                    )
                    pst_storage.remove_staging(
                        pst_storage.staging_root(storage_root, incomplete_uuid)
                    )

            import_record = resolution.import_record
            reimported = resolution.action is ImportJobAction.REIMPORT
            if resolution.action is ImportJobAction.RESUME and import_record is not None:
                import_id = int(import_record["id"])
                import_uuid = str(import_record["import_uuid"])
                account_id = str(import_record["account_id"])
            else:
                import_uuid = str(uuid.uuid4())
                account_id = (
                    f"pst_{source_snapshot.source_sha256[:12]}_{import_uuid.replace('-', '')[:8]}"
                )
                self._repository_factory().upsert_account(
                    {
                        "id": account_id,
                        "provider_type": "pst_import",
                        "display_name": display_name,
                        "is_enabled": 0,
                    }
                )
                import_id = repository.create_import(
                    {
                        "import_uuid": import_uuid,
                        "account_id": account_id,
                        "source_filename": pst_storage.source_filename(source),
                        "source_sha256": source_snapshot.source_sha256,
                        "source_size_bytes": source_snapshot.size_bytes,
                        "source_mtime": source_snapshot.mtime_ns,
                        "status": "extracting",
                        "is_active": 0 if reimported else 1,
                        "replaces_id": resolution.replaces_id,
                    }
                )

            pst_manifest = self._pst_manifest_factory(import_uuid)
            try:
                from mail_dock.domain.importer import ImportOptions

                options = ImportOptions(display_name, charset, include_deleted)
                if resolution.action is not ImportJobAction.RESUME:
                    get_version = getattr(importer, "get_version", None)
                    if not callable(get_version):
                        raise RuntimeError("PST importer does not expose its converter version")
                    readpst_version = str(get_version())
                    run_stage_a(
                        repository,
                        pst_manifest,
                        importer,
                        import_id=import_id,
                        import_uuid=import_uuid,
                        account_id=account_id,
                        source=source,
                        storage_root=cast(Any, self._storage_factory()).root,
                        source_snapshot=source_snapshot,
                        readpst_version=readpst_version,
                        options=options,
                        cancel=token,
                        on_progress=lambda count: self.progress.emit(
                            PstImportProgress("stage_a", count)
                        ),
                        pst_storage=pst_storage,
                    )
                stage_b: StageBResult = run_stage_b(
                    repository,
                    pst_manifest,
                    self._storage_factory(),
                    import_id=import_id,
                    import_uuid=import_uuid,
                    account_id=account_id,
                    staging_root=pst_storage.staging_root(
                        cast(Any, self._storage_factory()).root, import_uuid
                    ),
                    cancel=token,
                    storage_state=None,
                    pst_storage=pst_storage,
                )
                if reimported and resolution.replaces_id is not None and resolution.import_record:
                    old_uuid = str(resolution.import_record["import_uuid"])
                    old_manifest = self._pst_manifest_factory(old_uuid)
                    try:
                        switch_generation(
                            repository,
                            pst_manifest,
                            self._storage_factory(),
                            import_id=import_id,
                            import_uuid=import_uuid,
                            replaces_id=resolution.replaces_id,
                            replaces_import_uuid=old_uuid,
                        )
                    finally:
                        _close_manifest(old_manifest)
                return _SyncTaskResult(
                    "pst_import",
                    PstImportResult(
                        stage_b.status,
                        import_id,
                        stage_b.ingested_count,
                        stage_b.failed_count,
                        stage_b.remaining_count,
                        reimported,
                    ),
                )
            finally:
                _close_manifest(pst_manifest)

        return self._submit_operation("pst_import", operation)

    def prepare_attachment_save(
        self,
        *,
        relative_path: str,
        expected_hash: str,
        part_index: int,
        dest_dir: Path,
        filename: str | None = None,
    ) -> CancelToken:
        """Prepare an attachment without creating a destination file."""

        def operation(token: CancelToken) -> _SyncTaskResult:
            token.raise_if_cancelled()
            return _SyncTaskResult(
                "prepare_attachment",
                prepare_attachment_save(
                    self._storage_factory(),
                    self._message_renderer(),
                    relative_path=relative_path,
                    expected_hash=expected_hash,
                    part_index=part_index,
                    dest_dir=dest_dir,
                    filename=filename,
                ),
            )

        return self._submit_operation("prepare_attachment", operation)

    def commit_attachment_save(
        self,
        plan: AttachmentSavePlan,
        *,
        overwrite: bool = False,
    ) -> CancelToken:
        """Commit a reviewed attachment plan on the write worker."""

        def operation(token: CancelToken) -> _SyncTaskResult:
            token.raise_if_cancelled()
            return _SyncTaskResult(
                "save_attachment",
                commit_attachment_save(
                    self._storage_factory(),
                    self._message_renderer(),
                    plan=plan,
                    overwrite=overwrite,
                ),
            )

        return self._submit_operation("save_attachment", operation)

    def export_eml(
        self,
        *,
        relative_path: str,
        expected_hash: str,
        dest_path: Path,
    ) -> CancelToken:
        """Export one verified EML on the write worker."""

        def operation(token: CancelToken) -> _SyncTaskResult:
            token.raise_if_cancelled()
            return _SyncTaskResult(
                "export_eml",
                export_eml(
                    self._storage_factory(),
                    relative_path=relative_path,
                    expected_hash=expected_hash,
                    dest_path=dest_path,
                ),
            )

        return self._submit_operation("export_eml", operation)

    def export_mbox(
        self,
        *,
        message_ids: tuple[int, ...],
        dest_path: Path,
    ) -> CancelToken:
        """Export selected message IDs to mbox on the write worker."""

        def operation(token: CancelToken) -> _SyncTaskResult:
            return _SyncTaskResult(
                "export_mbox",
                export_mbox(
                    self._repository_factory(),
                    self._storage_factory(),
                    message_ids=message_ids,
                    dest_path=dest_path,
                    cancel=token,
                    on_progress=self._forward_export_progress(),
                ),
            )

        return self._submit_operation("export_mbox", operation)

    def export_attachments(
        self,
        *,
        message_ids: tuple[int, ...],
        dest_dir: Path,
    ) -> CancelToken:
        """Extract attachments for selected message IDs on the write worker."""

        def operation(token: CancelToken) -> _SyncTaskResult:
            repository = self._repository_factory()
            messages = tuple(
                message
                for message_id in message_ids
                if (message := repository.get_message(message_id)) is not None
            )
            return _SyncTaskResult(
                "export_attachments",
                export_attachments(
                    self._storage_factory(),
                    self._message_renderer(),
                    messages=messages,
                    dest_dir=dest_dir,
                    cancel=token,
                    on_progress=self._forward_export_progress(),
                ),
            )

        return self._submit_operation("export_attachments", operation)

    def restore_from_trash(self, message_ids: tuple[int, ...]) -> CancelToken:
        """Restore selected messages on the single database writer thread."""

        def operation(_token: CancelToken) -> _SyncTaskResult:
            return _SyncTaskResult(
                "restore_from_trash",
                restore_from_trash(self._repository_factory(), message_ids=message_ids),
            )

        return self._submit_operation("restore_from_trash", operation)

    def purge_messages(
        self,
        message_ids: tuple[int, ...],
        storage_state: object,
    ) -> CancelToken:
        """Permanently remove selected messages on the single writer thread."""

        def operation(_token: CancelToken) -> _SyncTaskResult:
            repository = self._repository_factory()
            storage = cast(BasePurgeStorage, self._storage_factory())
            pst_repository = (
                self._pst_import_repository_factory()
                if self._pst_import_repository_factory is not None
                else None
            )
            records_by_manifest: dict[tuple[str, str], list[int]] = {}
            for message_id in message_ids:
                record = repository.get_message(message_id)
                account_id = record.get("account_id") if record is not None else None
                if isinstance(account_id, str):
                    if record is not None and record.get("remote_state") == "no_remote":
                        import_uuid = (
                            pst_repository.get_import_uuid_for_message(message_id)
                            if pst_repository is not None
                            else None
                        )
                        if not isinstance(import_uuid, str):
                            continue
                        key = ("pst", import_uuid)
                    else:
                        key = ("imap", account_id)
                    records_by_manifest.setdefault(key, []).append(message_id)

            purged_ids: list[Any] = []
            skipped_ids: list[Any] = []
            physical_paths: list[str] = []
            shared_paths: list[str] = []
            total_size_bytes = 0
            for kind, manifest_id in records_by_manifest:
                account_message_ids = records_by_manifest[(kind, manifest_id)]
                manifest = (
                    self._pst_manifest_factory(manifest_id)
                    if kind == "pst" and self._pst_manifest_factory is not None
                    else self._manifest_factory(manifest_id)
                )
                try:
                    result = purge(
                        repository,
                        storage,
                        manifest,
                        message_ids=account_message_ids,
                        storage_state=cast(Any, storage_state),
                    )
                finally:
                    _close_manifest(manifest)
                purged_ids.extend(result.purged_ids)
                skipped_ids.extend(result.skipped_ids)
                physical_paths.extend(result.physically_deleted_paths)
                shared_paths.extend(result.shared_paths)
                total_size_bytes += result.total_size_bytes
            return _SyncTaskResult(
                "purge",
                PurgeResult(
                    purged_ids=tuple(purged_ids),
                    skipped_ids=tuple(skipped_ids),
                    physically_deleted_paths=tuple(physical_paths),
                    shared_paths=tuple(shared_paths),
                    total_size_bytes=total_size_bytes,
                ),
            )

        return self._submit_operation("purge", operation)

    def dry_run_remote_delete(
        self,
        message_ids: tuple[int, ...],
        storage_state: object,
    ) -> CancelToken:
        """Build a remote-delete plan without contacting the IMAP server."""

        def operation(_token: CancelToken) -> _SyncTaskResult:
            return _SyncTaskResult(
                "remote_delete_dry_run",
                dry_run(
                    self._repository_factory(),
                    self._storage_factory(),
                    message_ids=message_ids,
                    storage_state=cast(Any, storage_state),
                ),
            )

        return self._submit_operation("remote_delete_dry_run", operation)

    def execute_remote_delete(
        self,
        plan: DeleteDryRunResult,
        storage_state: object,
        *,
        mode: str = "trash",
        delete_batch_limit: int = 1000,
    ) -> CancelToken:
        """Execute a reviewed plan grouped by account on the write worker."""

        def operation(_token: CancelToken) -> _SyncTaskResult:
            repository = self._repository_factory()
            candidates_by_account: dict[str, list[Any]] = {}
            for candidate in plan.candidates:
                candidates_by_account.setdefault(candidate.account_id, []).append(candidate)

            completed_ids: list[Any] = []
            uncertain_ids: list[Any] = []
            skipped_ids: list[Any] = []
            errors: list[tuple[Any, str]] = []
            total_size_bytes = 0
            for account_id, candidates in candidates_by_account.items():
                account = _find_account(repository, account_id)
                fetcher = self._fetcher_factory(account)
                manifest = self._manifest_factory(account_id)
                try:
                    with fetcher:
                        result = execute(
                            fetcher,
                            repository,
                            self._storage_factory(),
                            manifest,
                            plan=tuple(candidates),
                            mode=mode,
                            storage_state=cast(Any, storage_state),
                            delete_batch_limit=delete_batch_limit,
                        )
                finally:
                    _close_manifest(manifest)
                completed_ids.extend(result.completed_ids)
                uncertain_ids.extend(result.uncertain_ids)
                skipped_ids.extend(result.skipped_ids)
                errors.extend(result.errors)
                total_size_bytes += result.total_size_bytes
            return _SyncTaskResult(
                "remote_delete",
                DeleteResult(
                    completed_ids=tuple(completed_ids),
                    uncertain_ids=tuple(uncertain_ids),
                    skipped_ids=tuple(skipped_ids),
                    errors=tuple(errors),
                    total_size_bytes=total_size_bytes,
                ),
            )

        return self._submit_operation("remote_delete", operation)

    def _message_renderer(self) -> BaseMessageRenderer:
        if self._renderer_factory is None:
            raise RuntimeError("file operations require a message renderer factory")
        return self._renderer_factory()

    def _submit_operation(
        self,
        operation_kind: SyncOperation,
        operation: Callable[[CancelToken], _SyncTaskResult],
    ) -> CancelToken:
        """Submit an operation while making its token available to callbacks."""

        token = CancelToken()

        def run() -> _SyncTaskResult:
            token.raise_if_cancelled()
            with operation_gate(self._operation_gate, token):
                return operation(token)

        self._operations_by_token[token] = operation_kind
        try:
            return self.submit(run, token)
        except BaseException:
            self._operations_by_token.pop(token, None)
            raise

    def _forward_progress(self) -> Callable[[SyncProgress], None]:
        """Create a per-operation progress relay throttled to 100ms."""

        last_emitted: float | None = None

        def forward(progress: SyncProgress) -> None:
            nonlocal last_emitted
            now = self._clock()
            if last_emitted is not None and now - last_emitted < 0.1:
                return
            last_emitted = now
            self.sync_progress.emit(progress)
            self.progress.emit(progress)

        return forward

    def _forward_export_progress(
        self,
    ) -> Callable[[ExportMboxProgress | ExportAttachmentsProgress], None]:
        """Relay export progress using the same 100ms throttling contract."""

        last_emitted: float | None = None

        def forward(progress: ExportMboxProgress | ExportAttachmentsProgress) -> None:
            nonlocal last_emitted
            now = self._clock()
            if last_emitted is not None and now - last_emitted < 0.1:
                return
            last_emitted = now
            self.progress.emit(progress)

        return forward

    def _emit_task_result(self, task: _Task, value: object) -> None:
        if not isinstance(value, _SyncTaskResult):
            super()._emit_task_result(task, value)
            return
        if value.operation == "sync":
            self.sync_result.emit(value.value)
        elif value.operation in {
            "prepare_attachment",
            "save_attachment",
            "export_eml",
            "export_mbox",
            "export_attachments",
        }:
            self.file_result.emit(value.value)
        elif value.operation == "restore_from_trash":
            self.trash_result.emit(value.value)
        elif value.operation == "purge":
            self.purge_result.emit(value.value)
        elif value.operation == "remote_delete_dry_run":
            self.delete_dry_run_result.emit(value.value)
        elif value.operation == "remote_delete":
            self.remote_delete_result.emit(value.value)
        elif value.operation == "pst_import":
            self.pst_import_result.emit(value.value)
        elif value.operation == "pst_import_decision":
            self.pst_import_decision_required.emit(value.value)
        elif value.operation == "failures_for_review":
            self.failures_for_review_result.emit(value.value)
        elif value.operation == "audit_log":
            self.audit_log_result.emit(value.value)
        elif value.operation in {"force_fetch", "reparse"}:
            self.failure_action_result.emit(value.value)
        elif isinstance(value.value, _FolderRefreshTaskResult):
            self.folders_refreshed.emit(value.value.result)
            self.folder_tree_updated.emit(value.value.snapshot)
        else:
            self.folder_tree_updated.emit(value.value)
        super()._emit_task_result(task, value.value)

    def _emit_task_failed(self, task: _Task, error: MailDockError) -> None:
        operation = self._operations_by_token.get(task.token)
        if operation is not None:
            notification = SyncErrorNotification(
                operation=operation,
                error=error,
                message=user_message(error),
            )
            self.error_reported.emit(notification)
            if isinstance(error, StorageDetachedError):
                self.storage_detached.emit(error)
            elif isinstance(error, AuthenticationError):
                self.authentication_failed.emit(notification)
            elif isinstance(error, FetchError):
                self.fetch_failed.emit(notification)
        super()._emit_task_failed(task, error)

    def _emit_task_cancelled(self, task: _Task) -> None:
        if self._operations_by_token.get(task.token) == "sync":
            self.sync_result.emit(SyncResult(0, 0, 0, 0, True))
        elif self._operations_by_token.get(task.token) == "pst_import":
            self.pst_import_cancelled.emit(task.token)
        super()._emit_task_cancelled(task)

    def _on_task_failed(self, token: object, _error: object) -> None:
        if isinstance(token, CancelToken):
            self._operations_by_token.pop(token, None)

    def _on_task_completed(self, token: object) -> None:
        if isinstance(token, CancelToken):
            self._operations_by_token.pop(token, None)

    def stop(self) -> None:
        """Cancel all operations before waiting for the worker thread."""

        try:
            super().stop()
        finally:
            self._operations_by_token.clear()


def _as_repository_factory(
    repository: RepositoryFactory | BaseMessageRepository,
) -> RepositoryFactory:
    if callable(repository) and not hasattr(repository, "list_accounts"):
        return repository
    fixed_repository = cast(BaseMessageRepository, repository)
    return lambda: fixed_repository


def _find_account(repository: BaseMessageRepository, account_id: str) -> MessageRecord:
    for account in repository.list_accounts():
        if account.get("id") == account_id:
            return account
    raise DatabaseError(f"Account does not exist: {account_id}")


def _account_id(account: MessageRecord) -> str | None:
    value = account.get("id", account.get("account_id"))
    return value if isinstance(value, str) and value else None


def _is_enabled_account(account: MessageRecord) -> bool:
    value = account.get("is_enabled", 1)
    return value not in (False, 0, "0")


def _add_sync_results(left: SyncResult, right: SyncResult) -> SyncResult:
    return SyncResult(
        left.fetched_count + right.fetched_count,
        left.transferred_bytes + right.transferred_bytes,
        left.skipped_count + right.skipped_count,
        left.failed_count + right.failed_count,
        left.cancelled or right.cancelled,
    )


def _close_manifest(manifest: BaseManifestWriter | BasePstManifestWriter) -> None:
    close = getattr(manifest, "close", None)
    if callable(close):
        close()


def _folder_tree_snapshot(
    repository: BaseMessageRepository,
    pst_import_repository: Callable[[], BasePstImportRepository] | None = None,
) -> FolderTreeSnapshot:
    """Read all tree data while the repository's worker thread owns the DB."""

    accounts = tuple(repository.list_accounts())
    folders = tuple(
        folder
        for account in accounts
        if (account_id := _account_id(account)) is not None
        for folder in repository.list_folders(account_id)
    )
    pst_imports: tuple[MessageRecord, ...] = ()
    if pst_import_repository is not None:
        pst_repository = pst_import_repository()
        list_imports = getattr(pst_repository, "list_imports", None)
        if callable(list_imports):
            pst_imports = tuple(list_imports())
    return FolderTreeSnapshot(accounts=accounts, folders=folders, pst_imports=pst_imports)


def _required_string(record: MessageRecord, key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise DatabaseError(f"Failure record has no {key}")
    return value


def _required_int(record: MessageRecord, key: str) -> int:
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise DatabaseError(f"Failure record has no valid {key}")
    return value
