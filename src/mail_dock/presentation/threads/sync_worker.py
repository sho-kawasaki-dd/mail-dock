"""Asynchronous synchronization worker for the GUI.

The worker creates repositories, fetchers, storage adapters, and manifest
writers inside its dedicated thread.  The GUI owns the ``CancelToken``
returned by each request and can therefore cancel a blocking fetch directly.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, cast

from PySide6.QtCore import Signal

from mail_dock.domain.errors import (
    AuthenticationError,
    DatabaseError,
    FetchError,
    MailDockError,
    StorageDetachedError,
)
from mail_dock.domain.fetcher import BaseMailFetcher, CancelToken
from mail_dock.domain.ports import BaseEmlStorage, BaseManifestWriter
from mail_dock.domain.repository import BaseMessageRepository, MessageRecord
from mail_dock.presentation.errors import user_message
from mail_dock.presentation.threads.worker import Worker, _Task
from mail_dock.usecases.sync_folders import FolderRefreshResult, refresh_folders
from mail_dock.usecases.sync_mail import SyncOptions, SyncProgress, SyncResult, sync_account

SyncOperation = Literal["sync", "refresh_folders"]
RepositoryFactory = Callable[[], BaseMessageRepository]
FetcherFactory = Callable[[MessageRecord], BaseMailFetcher]
StorageFactory = Callable[[], BaseEmlStorage]
ManifestFactory = Callable[[str], BaseManifestWriter]
SyncAccountUseCase = Callable[..., SyncResult]
RefreshFoldersUseCase = Callable[..., FolderRefreshResult]
Clock = Callable[[], float]


@dataclass(frozen=True)
class SyncErrorNotification:
    """A worker failure with presentation-safe user-facing text."""

    operation: SyncOperation
    error: MailDockError
    message: str


@dataclass(frozen=True)
class _SyncTaskResult:
    """Internal result envelope used to route results to typed signals."""

    operation: SyncOperation
    value: SyncResult | FolderRefreshResult


class SyncWorker(Worker):
    """Run account synchronization and folder refreshes on one QThread."""

    sync_progress = Signal(object)
    progress = Signal(object)
    sync_result = Signal(object)
    folders_refreshed = Signal(object)
    error_reported = Signal(object)
    authentication_failed = Signal(object)
    fetch_failed = Signal(object)
    storage_detached = Signal(object)

    def __init__(
        self,
        repository: RepositoryFactory | BaseMessageRepository,
        fetcher_factory: FetcherFactory,
        storage_factory: StorageFactory,
        manifest_factory: ManifestFactory,
        *,
        sync_account_usecase: SyncAccountUseCase = sync_account,
        refresh_folders_usecase: RefreshFoldersUseCase = refresh_folders,
        sync_options: SyncOptions | None = None,
        connection_manager: Any | None = None,
        clock: Clock = time.monotonic,
    ) -> None:
        super().__init__(connection_manager)
        self._repository_factory = _as_repository_factory(repository)
        self._fetcher_factory = fetcher_factory
        self._storage_factory = storage_factory
        self._manifest_factory = manifest_factory
        self._sync_account_usecase = sync_account_usecase
        self._refresh_folders_usecase = refresh_folders_usecase
        self._sync_options = sync_options or SyncOptions()
        self._clock = clock
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
            repository = self._repository_factory()
            account = _find_account(repository, account_id)
            fetcher = self._fetcher_factory(account)
            storage = self._storage_factory()
            manifest = self._manifest_factory(account_id)
            try:
                with fetcher:
                    result = self._sync_account_usecase(
                        fetcher,
                        repository,
                        storage,
                        manifest,
                        account_id=account_id,
                        options=selected_options,
                        cancel=token,
                        on_progress=self._forward_progress(),
                    )
                return _SyncTaskResult("sync", result)
            finally:
                _close_manifest(manifest)

        return self._submit_operation("sync", operation)

    request_sync_account = sync_account

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
            return _SyncTaskResult("refresh_folders", result)

        return self._submit_operation("refresh_folders", operation)

    request_refresh_folders = refresh_folders

    def _submit_operation(
        self,
        operation_kind: SyncOperation,
        operation: Callable[[CancelToken], _SyncTaskResult],
    ) -> CancelToken:
        """Submit an operation while making its token available to callbacks."""

        token = CancelToken()

        def run() -> _SyncTaskResult:
            token.raise_if_cancelled()
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

    def _emit_task_result(self, task: _Task, value: object) -> None:
        if not isinstance(value, _SyncTaskResult):
            super()._emit_task_result(task, value)
            return
        if value.operation == "sync":
            self.sync_result.emit(value.value)
        else:
            self.folders_refreshed.emit(value.value)
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


def _close_manifest(manifest: BaseManifestWriter) -> None:
    close = getattr(manifest, "close", None)
    if callable(close):
        close()
