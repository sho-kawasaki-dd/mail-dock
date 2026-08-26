"""Asynchronous integrity verification and metadata-cache worker."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from PySide6.QtCore import Signal

from mail_dock.domain.errors import MailDockError, StorageDetachedError
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.ports import (
    BaseEmlStorage,
    BaseIntegrityStorage,
    BaseManifestReader,
)
from mail_dock.domain.repository import BaseMessageRepository
from mail_dock.presentation.errors import user_message
from mail_dock.presentation.threads.worker import OperationGate, Worker, _Task, operation_gate
from mail_dock.usecases.reindex import ReindexProgress, ReindexResult, reindex
from mail_dock.usecases.reparse import ReparseResult, reparse_messages
from mail_dock.usecases.verify import (
    FullVerifyResult,
    ManifestVerifyResult,
    OrphanScanResult,
    QuickVerifyResult,
    RangeVerifyResult,
    VerifyProgress,
    full_verify,
    orphan_scan,
    quick_verify,
    range_verify,
    verify_manifest,
)

VerifyOperation = Literal[
    "quick_verify",
    "range_verify",
    "full_verify",
    "orphan_scan",
    "verify_manifest",
    "reindex",
    "reparse",
]
RepositoryFactory = Callable[[], BaseMessageRepository]
StorageFactory = Callable[[], BaseIntegrityStorage]
ManifestReaderFactory = Callable[[], BaseManifestReader]
ProgressCallback = Callable[[VerifyProgress | ReindexProgress], None]
ExclusiveWriteGuard = Callable[[], None]
ReindexCoordinator = Callable[..., ReindexResult]
Clock = Callable[[], float]
VerifyResult = (
    QuickVerifyResult
    | RangeVerifyResult
    | FullVerifyResult
    | OrphanScanResult
    | ManifestVerifyResult
    | ReindexResult
    | ReparseResult
)


@dataclass(frozen=True)
class VerifyErrorNotification:
    """A verification failure with presentation-safe user-facing text."""

    operation: VerifyOperation
    error: MailDockError
    message: str


@dataclass(frozen=True)
class VerifyTaskResult:
    """A completed operation tagged with the operation that produced it."""

    operation: VerifyOperation
    value: (
        QuickVerifyResult
        | RangeVerifyResult
        | FullVerifyResult
        | OrphanScanResult
        | ManifestVerifyResult
        | ReindexResult
        | ReparseResult
    )


class VerifyWorker(Worker):
    """Run verification work on a dedicated thread without physical deletion.

    The storage factory is typed as ``BaseIntegrityStorage`` deliberately. In
    particular, this worker has no ``BasePurgeStorage`` dependency and cannot
    physically delete an EML. Operations whose current use cases repair
    repository state accept an optional guard; the application supplies a
    guard that confirms the SyncWorker is stopped before those operations run.
    """

    verify_progress = Signal(object)
    progress = Signal(object)
    verify_result = Signal(object)
    error_reported = Signal(object)
    storage_detached = Signal(object)

    def __init__(
        self,
        repository: RepositoryFactory | BaseMessageRepository,
        storage_factory: StorageFactory | BaseIntegrityStorage,
        manifest_reader_factory: ManifestReaderFactory | None = None,
        *,
        manifest_root: Path | None = None,
        quick_verify_usecase: Callable[..., QuickVerifyResult] = quick_verify,
        range_verify_usecase: Callable[..., RangeVerifyResult] = range_verify,
        full_verify_usecase: Callable[..., FullVerifyResult] = full_verify,
        orphan_scan_usecase: Callable[..., OrphanScanResult] = orphan_scan,
        verify_manifest_usecase: Callable[..., ManifestVerifyResult] = verify_manifest,
        reindex_usecase: Callable[..., ReindexResult] = reindex,
        reindex_coordinator: ReindexCoordinator | None = None,
        reparse_usecase: Callable[..., ReparseResult] = reparse_messages,
        exclusive_write_guard: ExclusiveWriteGuard | None = None,
        connection_manager: Any | None = None,
        operation_gate: OperationGate | None = None,
        clock: Clock = time.monotonic,
    ) -> None:
        super().__init__(connection_manager)
        self._repository_factory = _as_repository_factory(repository)
        self._storage_factory = _as_storage_factory(storage_factory)
        self._manifest_reader_factory = manifest_reader_factory
        self._manifest_root = manifest_root
        self._quick_verify_usecase = quick_verify_usecase
        self._range_verify_usecase = range_verify_usecase
        self._full_verify_usecase = full_verify_usecase
        self._orphan_scan_usecase = orphan_scan_usecase
        self._verify_manifest_usecase = verify_manifest_usecase
        self._reindex_usecase = reindex_usecase
        self._reindex_coordinator = reindex_coordinator
        self._reparse_usecase = reparse_usecase
        self._exclusive_write_guard = exclusive_write_guard
        self._operation_gate = operation_gate
        self._clock = clock
        self._operations_by_token: dict[CancelToken, VerifyOperation] = {}

        self.task_completed.connect(self._on_task_completed)

    def quick_verify(self) -> CancelToken:
        """Queue a path-existence and size verification."""

        return self._submit_operation(
            "quick_verify",
            lambda token: self._quick_verify_usecase(
                self._repository_factory(), self._storage_factory(), cancel=token
            ),
        )

    request_quick_verify = quick_verify

    def range_verify(self) -> CancelToken:
        """Queue checkpoint-bounded verification and its repair result."""

        def operation(token: CancelToken) -> RangeVerifyResult:
            self._ensure_exclusive_write()
            return self._range_verify_usecase(
                self._repository_factory(),
                self._storage_factory(),
                self._manifest_reader(),
                cancel=token,
            )

        return self._submit_operation("range_verify", operation)

    request_range_verify = range_verify

    def full_verify(self) -> CancelToken:
        """Queue a chunked SHA-256 verification of every stored EML."""

        return self._submit_operation(
            "full_verify",
            lambda token: self._full_verify_usecase(
                self._repository_factory(),
                self._storage_factory(),
                cancel=token,
                on_progress=self._forward_progress(),
            ),
        )

    request_full_verify = full_verify

    def orphan_scan(self) -> CancelToken:
        """Queue a provenance-aware orphan scan."""

        def operation(token: CancelToken) -> OrphanScanResult:
            self._ensure_exclusive_write()
            return self._orphan_scan_usecase(
                self._repository_factory(),
                self._storage_factory(),
                cancel=token,
                on_progress=self._forward_progress(),
                manifest_reader=self._manifest_reader(),
            )

        return self._submit_operation("orphan_scan", operation)

    request_orphan_scan = orphan_scan

    def verify_manifest(self, root: Path | None = None) -> CancelToken:
        """Queue manifest CRC validation and safe tail repair."""

        selected_root = root or self._manifest_root
        if selected_root is None:
            raise ValueError("verify_manifest requires a manifest root")

        return self._submit_operation(
            "verify_manifest",
            lambda token: self._verify_manifest_with_guard(selected_root, token),
        )

    request_verify_manifest = verify_manifest

    def reindex(self) -> CancelToken:
        """Queue replacement-database population from durable source data."""

        def operation(token: CancelToken) -> ReindexResult:
            self._ensure_exclusive_write()
            if self._reindex_coordinator is not None:
                return self._reindex_coordinator(
                    cancel=token,
                    on_progress=self._forward_progress(),
                )
            return self._reindex_usecase(
                self._repository_factory(),
                cast(BaseEmlStorage, self._storage_factory()),
                self._manifest_reader(),
                cancel=token,
                on_progress=self._forward_progress(),
            )

        return self._submit_operation("reindex", operation)

    request_reindex = reindex

    def reparse(
        self,
        *,
        account_id: str | None = None,
        only_failed: bool = True,
    ) -> CancelToken:
        """Queue reparsing of integrity-checked EML files."""

        def operation(token: CancelToken) -> ReparseResult:
            self._ensure_exclusive_write()
            return self._reparse_usecase(
                self._repository_factory(),
                cast(BaseEmlStorage, self._storage_factory()),
                account_id=account_id,
                only_failed=only_failed,
                cancel=token,
            )

        return self._submit_operation("reparse", operation)

    request_reparse = reparse
    reparse_messages = reparse

    def _manifest_reader(self) -> BaseManifestReader:
        if self._manifest_reader_factory is None:
            raise RuntimeError("this verification operation requires a manifest reader")
        return self._manifest_reader_factory()

    def _verify_manifest_with_guard(
        self,
        root: Path,
        token: CancelToken,
    ) -> ManifestVerifyResult:
        self._ensure_exclusive_write()
        return self._verify_manifest_usecase(root, cancel=token)

    def _ensure_exclusive_write(self) -> None:
        """Require the presentation layer to serialize repository mutations."""

        if self._exclusive_write_guard is None:
            raise RuntimeError(
                "write-capable verification operations require an exclusive write guard"
            )
        self._exclusive_write_guard()

    def _submit_operation(
        self,
        operation_kind: VerifyOperation,
        operation: Callable[[CancelToken], object],
    ) -> CancelToken:
        token = CancelToken()

        def run() -> VerifyTaskResult:
            token.raise_if_cancelled()
            with operation_gate(self._operation_gate, token):
                return VerifyTaskResult(operation_kind, cast(VerifyResult, operation(token)))

        self._operations_by_token[token] = operation_kind
        try:
            return self.submit(run, token)
        except BaseException:
            self._operations_by_token.pop(token, None)
            raise

    def _forward_progress(self) -> ProgressCallback:
        """Create a progress relay throttled to the shared 100ms rule."""

        last_emitted: float | None = None

        def forward(progress: VerifyProgress | ReindexProgress) -> None:
            nonlocal last_emitted
            now = self._clock()
            if last_emitted is not None and now - last_emitted < 0.1:
                return
            last_emitted = now
            self.verify_progress.emit(progress)
            self.progress.emit(progress)

        return forward

    def _emit_task_result(self, task: _Task, value: object) -> None:
        if isinstance(value, VerifyTaskResult):
            self.verify_result.emit(value.value)
            super()._emit_task_result(task, value)
            return
        super()._emit_task_result(task, value)

    def _emit_task_failed(self, task: _Task, error: MailDockError) -> None:
        operation = self._operations_by_token.get(task.token)
        if operation is not None:
            notification = VerifyErrorNotification(operation, error, user_message(error))
            self.error_reported.emit(notification)
            if isinstance(error, StorageDetachedError):
                self.storage_detached.emit(error)
        super()._emit_task_failed(task, error)

    def _on_task_completed(self, token: object) -> None:
        if isinstance(token, CancelToken):
            self._operations_by_token.pop(token, None)

    def stop(self) -> None:
        """Cancel queued work before waiting for the worker thread."""

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


def _as_storage_factory(
    storage: StorageFactory | BaseIntegrityStorage,
) -> StorageFactory:
    if callable(storage) and not hasattr(storage, "iter_chunks"):
        return storage
    fixed_storage = cast(BaseIntegrityStorage, storage)
    return lambda: fixed_storage


__all__ = ["VerifyErrorNotification", "VerifyTaskResult", "VerifyWorker"]
