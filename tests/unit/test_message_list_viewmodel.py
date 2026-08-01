from dataclasses import dataclass
from typing import Literal, cast

import pytest
from PySide6.QtCore import QObject, Signal

from mail_dock.domain.errors import SearchQueryError
from mail_dock.domain.search import MessageFilter, PageCursor
from mail_dock.presentation.viewmodels.message_list_viewmodel import (
    MessageListQueryWorker,
    MessageListViewModel,
)

pytestmark = pytest.mark.gui


@dataclass(frozen=True)
class Request:
    request_id: int


class FakeWorker(QObject):
    result = Signal(object)
    request_failed = Signal(object)
    request_cancelled = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.cancelled: list[str] = []
        self._next_id = 0

    def list_messages(
        self,
        *,
        filters: MessageFilter,
        cursor: PageCursor | None,
        limit: int,
    ) -> Request:
        return self._issue("list", filters=filters, cursor=cursor, limit=limit)

    def search_messages(
        self,
        *,
        query: str,
        mode: Literal["and", "or"],
        filters: MessageFilter,
        cursor: PageCursor | None,
        limit: int,
    ) -> Request:
        return self._issue(
            "search",
            query=query,
            mode=mode,
            filters=filters,
            cursor=cursor,
            limit=limit,
        )

    def cancel(self, channel: Literal["list/search"]) -> Request | None:
        self.cancelled.append(channel)
        return None

    def _issue(self, operation: str, **kwargs: object) -> Request:
        self._next_id += 1
        self.calls.append((operation, kwargs))
        return Request(self._next_id)


def test_state_signals_and_enter_search_request(qtbot: object) -> None:
    del qtbot
    worker = FakeWorker()
    viewmodel = MessageListViewModel(cast(MessageListQueryWorker, worker))
    changes: list[tuple[str, str]] = []
    requested: list[tuple[str, str]] = []
    selected: list[int | None] = []
    viewmodel.search_changed.connect(lambda query, mode: changes.append((query, mode)))
    viewmodel.search_requested.connect(lambda query, mode: requested.append((query, mode)))
    viewmodel.message_selected.connect(selected.append)

    viewmodel.set_search_query("  Invoice  ")
    assert worker.calls == []

    viewmodel.execute_search()
    assert worker.calls == [
        (
            "search",
            {
                "query": "  Invoice  ",
                "mode": "and",
                "filters": MessageFilter(),
                "cursor": None,
                "limit": 200,
            },
        )
    ]
    assert changes == [("  Invoice  ", "and"), ("  Invoice  ", "and")]
    assert requested == [("  Invoice  ", "and")]

    viewmodel.select_message(42)
    assert viewmodel.selected_message_id == 42
    assert selected == [42]


def test_stale_result_is_ignored_and_filter_requests_a_new_page(qtbot: object) -> None:
    del qtbot
    worker = FakeWorker()
    viewmodel = MessageListViewModel(cast(MessageListQueryWorker, worker))
    received: list[object] = []
    viewmodel.result_received.connect(received.append)

    first = viewmodel.request_page()
    viewmodel.set_filters(MessageFilter(account_ids=("account-2",)))
    assert worker.cancelled == ["list/search"]
    second = Request(worker._next_id)
    worker.result.emit(
        type("Result", (), {"channel": "list/search", "request_id": first.request_id})()
    )
    assert received == []

    worker.result.emit(
        type("Result", (), {"channel": "list/search", "request_id": second.request_id})()
    )
    assert len(received) == 1
    assert viewmodel.filters == MessageFilter(account_ids=("account-2",))


def test_search_feedback_tracks_slow_path_and_query_error(qtbot: object) -> None:
    del qtbot
    worker = FakeWorker()
    viewmodel = MessageListViewModel(cast(MessageListQueryWorker, worker))
    errors: list[str] = []
    slow_paths: list[bool] = []
    busy: list[bool] = []
    viewmodel.search_error_changed.connect(errors.append)
    viewmodel.slow_path_changed.connect(slow_paths.append)
    viewmodel.request_busy_changed.connect(busy.append)

    viewmodel.set_search_query("in")
    request = viewmodel.execute_search()
    assert busy == [True]
    worker.result.emit(
        type(
            "Result",
            (),
            {
                "channel": "list/search",
                "request_id": request.request_id,
                "value": type("Page", (), {"has_slow_path": True})(),
            },
        )()
    )
    assert slow_paths == [True]
    assert busy == [True, False]

    viewmodel.set_search_query('"unterminated')
    request = viewmodel.execute_search()
    worker.request_failed.emit(
        type(
            "Failure",
            (),
            {
                "channel": "list/search",
                "request_id": request.request_id,
                "error": SearchQueryError("invalid query"),
            },
        )()
    )
    assert errors == ["検索条件が無効です。"]
