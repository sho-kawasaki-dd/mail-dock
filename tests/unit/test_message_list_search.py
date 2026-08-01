from dataclasses import dataclass
from typing import Literal, cast

import pytest
from PySide6.QtCore import QObject, Signal

from mail_dock.domain.search import MessageFilter, MessageSummary, PageCursor
from mail_dock.presentation.models.message_table_model import (
    MessageQueryWorker,
    MessageTableModel,
    MessageThreadQueryWorker,
)
from mail_dock.presentation.viewmodels.message_list_viewmodel import (
    MessageListQueryWorker,
    MessageListViewModel,
)
from mail_dock.presentation.views.message_list import MessageListSearchBar, MessageListView

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
            "search", query=query, mode=mode, filters=filters, cursor=cursor, limit=limit
        )

    def list_thread(
        self,
        *,
        thread_key: str,
        filters: MessageFilter | None = None,
    ) -> Request:
        return self._issue("thread", thread_key=thread_key, filters=filters)

    def cancel(self, channel: str) -> Request | None:
        self.calls.append(("cancel", {"channel": channel}))
        return None

    def _issue(self, operation: str, **kwargs: object) -> Request:
        self._next_id += 1
        self.calls.append((operation, kwargs))
        return Request(self._next_id)

    def emit_thread(self, request_id: int, items: tuple[MessageSummary, ...]) -> None:
        self.result.emit(
            type(
                "Result",
                (),
                {
                    "channel": "count/thread",
                    "request_id": request_id,
                    "value": items,
                },
            )()
        )


def test_enter_only_search_and_structured_filters(qtbot: object) -> None:
    del qtbot
    worker = FakeWorker()
    viewmodel = MessageListViewModel(cast(MessageListQueryWorker, worker))
    search_bar = MessageListSearchBar(viewmodel)

    search_bar.search_input.setText("  Invoice  ")
    assert worker.calls == []

    search_bar.mode_selector.setCurrentIndex(1)
    search_bar.search_input.returnPressed.emit()
    assert worker.calls[-1] == (
        "search",
        {
            "query": "  Invoice  ",
            "mode": "or",
            "filters": MessageFilter(),
            "cursor": None,
            "limit": 200,
        },
    )

    search_bar._from_enabled.setChecked(True)
    assert worker.calls[-1][0] == "search"
    filters = cast(MessageFilter, worker.calls[-1][1]["filters"])
    assert filters.date_from is not None

    search_bar._attachment_selector.setCurrentIndex(1)
    filters = cast(MessageFilter, worker.calls[-1][1]["filters"])
    assert filters.has_attachment is True


def test_message_list_view_configures_table_and_loads_thread(qtbot: object) -> None:
    del qtbot
    worker = FakeWorker()
    model = MessageTableModel(cast(MessageQueryWorker, worker))
    view = MessageListView(
        model,
        worker=cast(MessageThreadQueryWorker, worker),
    )

    assert view.selectionBehavior() == view.SelectionBehavior.SelectRows
    assert view.isSortingEnabled() is False
    assert view.verticalHeader().sectionResizeMode(0) == view.verticalHeader().ResizeMode.Fixed

    request = view.show_thread("thread-1")
    assert worker.calls[-1] == ("thread", {"thread_key": "thread-1", "filters": None})
    worker.emit_thread(request.request_id, ())
    assert model.thread_mode is True
    assert model.rowCount() == 0
    assert model.canFetchMore() is False

    view.clear_thread()
    assert model.thread_mode is False
    assert model.canFetchMore() is True