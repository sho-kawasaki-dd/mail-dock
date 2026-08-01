from dataclasses import dataclass
from typing import Literal, cast

import pytest
from PySide6.QtCore import QObject, Signal

from mail_dock.domain.search import MessageFilter, PageCursor
from mail_dock.presentation.viewmodels.message_list_viewmodel import (
    MessageListQueryWorker,
    MessageListViewModel,
)
from mail_dock.presentation.views.message_list import MessageListSearchBar

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

    def cancel(self, channel: Literal["list/search"]) -> Request | None:
        del channel
        return None

    def _issue(self, operation: str, **kwargs: object) -> Request:
        self._next_id += 1
        self.calls.append((operation, kwargs))
        return Request(self._next_id)


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
    assert worker.calls[-1][1]["filters"].date_from is not None

    search_bar._attachment_selector.setCurrentIndex(1)
    assert worker.calls[-1][1]["filters"].has_attachment is True