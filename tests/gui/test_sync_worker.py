from __future__ import annotations

from threading import Event
from types import SimpleNamespace
from typing import Any, cast

import pytest
from PySide6.QtCore import Qt

from mail_dock.domain.errors import OperationCancelledError
from mail_dock.domain.fetcher import BaseMailFetcher
from mail_dock.domain.ports import BaseEmlStorage, BaseManifestWriter
from mail_dock.domain.repository import BaseMessageRepository
from mail_dock.presentation.threads import sync_worker as sync_worker_module
from mail_dock.presentation.threads.sync_worker import SyncWorker
from mail_dock.usecases.delete_remote import DeleteDryRunResult, DeleteResult, DeleteScope
from mail_dock.usecases.sync_mail import SyncProgress, SyncResult

pytestmark = pytest.mark.gui


class _Fetcher:
    def __enter__(self) -> _Fetcher:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _Repository:
    def list_accounts(self) -> list[dict[str, object]]:
        return [{"id": "account-1"}]


class _RepositoryWithPst:
    def list_accounts(self) -> list[dict[str, object]]:
        return [
            {"id": "account-1", "provider_type": "onamae_imap"},
            {"id": "pst-1", "provider_type": "pst_import"},
        ]


def _worker(
    *, sync_usecase: Any, clock: Any = lambda: 0.0, repository: Any | None = None
) -> SyncWorker:
    return SyncWorker(
        cast(BaseMessageRepository, repository or _Repository()),
        lambda _account: cast(BaseMailFetcher, _Fetcher()),
        cast(Any, lambda: cast(BaseEmlStorage, object())),
        cast(Any, lambda _account_id: cast(BaseManifestWriter, object())),
        sync_account_usecase=sync_usecase,
        clock=clock,
    )


def test_progress_is_forwarded_at_most_once_per_100ms(qtbot: object) -> None:
    del qtbot
    now = [0.0]
    worker = _worker(
        sync_usecase=lambda *_args, **_kwargs: SyncResult(0, 0, 0, 0, False),
        clock=lambda: now[0],
    )
    received: list[object] = []
    worker.sync_progress.connect(received.append, Qt.ConnectionType.DirectConnection)
    forward = worker._forward_progress()
    progress = SyncProgress(1, 10, 1, "INBOX", None)

    forward(progress)
    now[0] = 0.05
    forward(progress)
    now[0] = 0.1
    forward(progress)

    assert received == [progress, progress]


def test_running_sync_observes_direct_token_cancellation(qtbot: Any) -> None:
    started = Event()
    cancelled = Event()

    def blocking_sync(*args: Any, **kwargs: Any) -> SyncResult:
        del args
        token = kwargs["cancel"]
        started.set()
        while not token.is_cancelled:
            cancelled.wait(0.01)
        cancelled.set()
        raise OperationCancelledError("cancelled")

    worker = _worker(sync_usecase=blocking_sync)
    results: list[object] = []
    worker.sync_result.connect(results.append)
    worker.start()

    try:
        token = worker.sync_account("account-1")
        qtbot.waitUntil(started.is_set, timeout=2_000)
        token.cancel()
        qtbot.waitUntil(cancelled.is_set, timeout=2_000)
        qtbot.waitUntil(lambda: bool(results), timeout=2_000)
        assert results == [SyncResult(0, 0, 0, 0, True)]
    finally:
        worker.stop()


def test_sync_all_accounts_skips_pst_archives(qtbot: Any) -> None:
    called: list[str] = []

    def sync_usecase(*args: Any, **kwargs: Any) -> SyncResult:
        called.append(str(kwargs["account_id"]))
        return SyncResult(0, 0, 0, 0, False)

    worker = _worker(
        sync_usecase=sync_usecase,
        repository=_RepositoryWithPst(),
    )
    results: list[object] = []
    worker.sync_result.connect(results.append)
    worker.start()

    try:
        worker.sync_all_accounts()
        qtbot.waitUntil(lambda: bool(results), timeout=2_000)
        assert called == ["account-1"]
    finally:
        worker.stop()


def test_remote_delete_worker_preserves_scope_settings_and_limit(
    qtbot: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = DeleteScope((1,), (), (), 1, 0, False, 3)
    candidate = cast(Any, SimpleNamespace(account_id="account-1"))
    scoped_plan = DeleteDryRunResult(
        candidates=(candidate,),
        exclude_flagged=True,
        scope=scope,
    )
    manual_plan = DeleteDryRunResult(candidates=(candidate,))
    dry_run_calls: list[dict[str, object]] = []
    execute_calls: list[dict[str, object]] = []

    def run_dry_run(*_args: object, **kwargs: object) -> DeleteDryRunResult:
        dry_run_calls.append(kwargs)
        return scoped_plan

    def run_execute(*_args: object, **kwargs: object) -> DeleteResult:
        execute_calls.append(kwargs)
        return DeleteResult()

    monkeypatch.setattr(sync_worker_module, "dry_run", run_dry_run)
    monkeypatch.setattr(sync_worker_module, "execute", run_execute)

    worker = _worker(sync_usecase=lambda *_args, **_kwargs: SyncResult(0, 0, 0, 0, False))
    dry_run_results: list[object] = []
    delete_results: list[object] = []
    worker.delete_dry_run_result.connect(dry_run_results.append)
    worker.remote_delete_result.connect(delete_results.append)
    worker.start()

    try:
        worker.dry_run_remote_delete(
            (1, 2),
            object(),
            folder_id=7,
            exclude_flagged=True,
            scope=scope,
        )
        worker.execute_remote_delete(scoped_plan, object(), delete_batch_limit=99)
        worker.execute_remote_delete(manual_plan, object(), delete_batch_limit=11)
        qtbot.waitUntil(
            lambda: len(dry_run_results) == 1 and len(delete_results) == 2,
            timeout=2_000,
        )
    finally:
        worker.stop()

    assert dry_run_calls[0]["message_ids"] == (1, 2)
    assert dry_run_calls[0]["folder_id"] == 7
    assert dry_run_calls[0]["exclude_flagged"] is True
    assert dry_run_calls[0]["scope"] is scope
    assert execute_calls[0]["delete_batch_limit"] == 3
    assert execute_calls[0]["exclude_flagged"] is True
    assert execute_calls[1]["delete_batch_limit"] == 11
    assert execute_calls[1]["exclude_flagged"] is False
