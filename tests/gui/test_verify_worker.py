from __future__ import annotations

from threading import Event
from typing import Any, cast

import pytest
from PySide6.QtCore import Qt

from mail_dock.domain.errors import StorageDetachedError
from mail_dock.domain.ports import BaseIntegrityStorage, BaseManifestReader
from mail_dock.domain.repository import BaseMessageRepository
from mail_dock.presentation.threads.verify_worker import VerifyWorker
from mail_dock.usecases.verify import (
    FullVerifyResult,
    QuickVerifyResult,
    RangeVerifyResult,
    VerifyProgress,
)

pytestmark = pytest.mark.gui


class _ConnectionManager:
    def __init__(self) -> None:
        self.closed = Event()

    def close_current_thread(self) -> None:
        self.closed.set()


def _worker(
    *,
    quick_usecase: Any = lambda *_args, **_kwargs: QuickVerifyResult(0, (), (), False),
    full_usecase: Any = lambda *_args, **_kwargs: FullVerifyResult(0, (), False),
    exclusive_write_guard: Any = None,
    connection_manager: Any = None,
) -> VerifyWorker:
    return VerifyWorker(
        cast(BaseMessageRepository, object()),
        cast(BaseIntegrityStorage, object()),
        cast(Any, lambda: cast(BaseManifestReader, object())),
        quick_verify_usecase=quick_usecase,
        full_verify_usecase=full_usecase,
        exclusive_write_guard=exclusive_write_guard,
        connection_manager=connection_manager,
    )


def test_progress_is_forwarded_at_most_once_per_100ms(qtbot: object) -> None:
    del qtbot
    now = [0.0]
    worker = _worker()
    worker._clock = lambda: now[0]
    received: list[object] = []
    worker.verify_progress.connect(received.append, Qt.ConnectionType.DirectConnection)
    forward = worker._forward_progress()
    progress = VerifyProgress(1, 10, "eml/message.eml")

    forward(progress)
    now[0] = 0.05
    forward(progress)
    now[0] = 0.1
    forward(progress)

    assert received == [progress, progress]


def test_quick_verify_emits_result(qtbot: Any) -> None:
    expected = QuickVerifyResult(2, ("missing.eml",), (), False)
    worker = _worker(quick_usecase=lambda *_args, **_kwargs: expected)
    results: list[object] = []
    worker.verify_result.connect(results.append)
    worker.start()

    try:
        worker.quick_verify()
        qtbot.waitUntil(lambda: results == [expected], timeout=2_000)
    finally:
        worker.stop()


def test_storage_detached_is_forwarded(qtbot: Any) -> None:
    def failing_verify(*_args: Any, **_kwargs: Any) -> FullVerifyResult:
        raise StorageDetachedError("detached")

    worker = _worker(full_usecase=failing_verify)
    detached: list[object] = []
    worker.storage_detached.connect(detached.append)
    worker.start()

    try:
        worker.full_verify()
        qtbot.waitUntil(lambda: bool(detached), timeout=2_000)
        assert isinstance(detached[0], StorageDetachedError)
    finally:
        worker.stop()


def test_running_operation_can_be_cancelled_with_its_returned_token(qtbot: Any) -> None:
    started = Event()
    cancelled = Event()
    release = Event()

    def blocking_verify(*_args: Any, **kwargs: Any) -> FullVerifyResult:
        token = cast(Any, kwargs["cancel"])
        started.set()
        while True:
            token.raise_if_cancelled()
            release.wait(0.01)

    worker = _worker(full_usecase=blocking_verify)
    worker.cancelled.connect(cancelled.set)
    worker.start()

    try:
        token = worker.full_verify()
        qtbot.waitUntil(started.is_set, timeout=2_000)
        token.cancel()
        qtbot.waitUntil(cancelled.is_set, timeout=2_000)
    finally:
        release.set()
        worker.stop()


def test_write_operation_checks_exclusive_guard(qtbot: Any) -> None:
    guard_calls: list[str] = []
    operation_calls: list[str] = []
    worker = VerifyWorker(
        cast(BaseMessageRepository, object()),
        cast(BaseIntegrityStorage, object()),
        cast(Any, lambda: cast(BaseManifestReader, object())),
        range_verify_usecase=lambda *_args, **_kwargs: (
            operation_calls.append("usecase") or RangeVerifyResult(0, (), 0, 0, False)
        ),
        exclusive_write_guard=lambda: guard_calls.append("checked"),
    )
    worker.start()

    try:
        worker.range_verify()
        qtbot.waitUntil(lambda: operation_calls == ["usecase"], timeout=2_000)
        assert guard_calls == ["checked"]
    finally:
        worker.stop()


def test_worker_closes_its_thread_connection_after_task(qtbot: Any) -> None:
    connection_manager = _ConnectionManager()
    worker = _worker(connection_manager=connection_manager)
    worker.start()

    try:
        worker.quick_verify()
        qtbot.waitUntil(connection_manager.closed.is_set, timeout=2_000)
    finally:
        worker.stop()
