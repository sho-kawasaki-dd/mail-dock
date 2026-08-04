from __future__ import annotations

from threading import Event
from typing import Any, cast

import pytest
from PySide6.QtCore import Qt

from mail_dock.domain.errors import OperationCancelledError
from mail_dock.domain.fetcher import BaseMailFetcher
from mail_dock.domain.ports import BaseEmlStorage, BaseManifestWriter
from mail_dock.domain.repository import BaseMessageRepository
from mail_dock.presentation.threads.sync_worker import SyncWorker
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


def _worker(*, sync_usecase: Any, clock: Any = lambda: 0.0) -> SyncWorker:
    return SyncWorker(
        cast(BaseMessageRepository, _Repository()),
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
