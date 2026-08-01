from __future__ import annotations

from threading import Event
from time import monotonic
from typing import cast

import pytest

from mail_dock.domain.errors import OperationCancelledError
from mail_dock.domain.search import BaseSearchRepository, MessageFilter, SearchPage
from mail_dock.presentation.threads.query_worker import QueryCancelled, QueryFailure, QueryWorker

pytestmark = pytest.mark.gui


class _BlockingSearchRepository:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()
        self.calls: list[str] = []
        self.raise_cancelled = False

    def list_messages(self, filters: MessageFilter, **kwargs: object) -> SearchPage:
        del filters, kwargs
        self.calls.append("list")
        self.started.set()
        while not self.release.wait(0.01):
            if self.raise_cancelled:
                raise OperationCancelledError("cancelled")
        return SearchPage((), None, True)

    def search_messages(self, *args: object, **kwargs: object) -> SearchPage:
        del args, kwargs
        self.calls.append("search")
        return SearchPage((), None, True)

    def count_messages(self, *args: object, **kwargs: object) -> int:
        del args, kwargs
        self.calls.append("count")
        return 0

    def list_thread(self, *args: object, **kwargs: object) -> tuple[object, ...]:
        del args, kwargs
        self.calls.append("thread")
        return ()

    def get_message(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        return None


def _wait_until(predicate: object, timeout: float = 2.0) -> bool:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        from PySide6.QtWidgets import QApplication

        application = QApplication.instance()
        if application is not None:
            application.processEvents()
        if callable(predicate) and predicate():
            return True
    return False


def test_ui_owned_token_cancels_a_running_query_without_worker_callback(qtbot: object) -> None:
    del qtbot
    repository = _BlockingSearchRepository()
    worker = QueryWorker(cast(BaseSearchRepository, repository))
    cancelled: list[QueryCancelled] = []
    failures: list[QueryFailure] = []
    worker.request_cancelled.connect(cancelled.append)
    worker.request_failed.connect(failures.append)
    worker.start()

    handle = worker.list_messages()
    assert repository.started.wait(1)
    handle.token.cancel()
    repository.raise_cancelled = True
    assert _wait_until(lambda: len(cancelled) == 1)
    assert failures == []
    assert cancelled[0].request_id == handle.request_id
    worker.stop()


def test_replacing_one_channel_does_not_cancel_another(qtbot: object) -> None:
    del qtbot
    repository = _BlockingSearchRepository()
    worker = QueryWorker(cast(BaseSearchRepository, repository))
    worker.start()

    list_handle = worker.list_messages()
    assert repository.started.wait(1)
    detail_handle = worker.count_messages()
    replacement = worker.search_messages(query="invoice")

    assert list_handle.request_id != replacement.request_id
    assert detail_handle.channel == "count/thread"
    assert worker.request_state.current("count/thread") == detail_handle
    assert worker.request_state.current("list/search") == replacement

    list_handle.token.cancel()
    repository.raise_cancelled = True
    repository.release.set()
    assert _wait_until(lambda: not worker.active_tokens)
    worker.stop()