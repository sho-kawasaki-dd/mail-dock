from __future__ import annotations

from threading import Event
from time import monotonic
from typing import cast

import pytest

from mail_dock.domain.errors import OperationCancelledError
from mail_dock.domain.search import BaseSearchRepository, MessageFilter, SearchPage
from mail_dock.presentation.threads.query_worker import (
    QueryCancelled,
    QueryFailure,
    QueryResult,
    QueryWorker,
)

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


def test_full_list_supports_an_independent_delete_channel(
    qtbot: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del qtbot
    export_started = Event()
    release_export = Event()
    result_channels: list[str] = []
    queries: list[str] = []

    def record_result(result: object) -> None:
        result_channels.append(cast(QueryResult, result).channel)

    def list_everything(_repository: object, **kwargs: object) -> tuple[()]:
        query = str(kwargs["query"])
        queries.append(query)
        if query == "export":
            export_started.set()
            assert release_export.wait(2)
        return ()

    monkeypatch.setattr(
        "mail_dock.presentation.threads.query_worker.list_all_messages", list_everything
    )
    worker = QueryWorker(cast(BaseSearchRepository, object()))
    worker.result.connect(record_result)
    worker.start()

    try:
        export_handle = worker.list_all_messages(query="export")
        assert _wait_until(export_started.is_set)
        delete_handle = worker.list_all_messages(query="delete", channel="delete/list")

        assert export_handle.channel == "export/list"
        assert delete_handle.channel == "delete/list"
        assert not export_handle.token.is_cancelled
        assert worker.request_state.current("export/list") == export_handle
        assert worker.request_state.current("delete/list") == delete_handle

        release_export.set()
        assert _wait_until(lambda: len(result_channels) == 2)
    finally:
        release_export.set()
        worker.stop()

    assert result_channels == ["export/list", "delete/list"]
    assert queries == ["export", "delete"]
