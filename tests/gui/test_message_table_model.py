from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Literal, cast

import pytest
from PySide6.QtCore import QObject, QTimer, Signal

from mail_dock.domain.search import MessageFilter, MessageSummary, PageCursor, SearchPage
from mail_dock.presentation.models.message_table_model import (
    MessageQueryWorker,
    MessageTableModel,
)

pytestmark = pytest.mark.gui


@dataclass(frozen=True)
class _Request:
    request_id: int


class _FakeQueryWorker(QObject):
    result = Signal(object)
    request_failed = Signal(object)
    request_cancelled = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.cancelled_channels: list[str] = []
        self._next_request_id = 0

    def list_messages(
        self,
        *,
        filters: MessageFilter,
        cursor: PageCursor | None,
        limit: int,
    ) -> _Request:
        return self._issue("list", filters=filters, cursor=cursor, limit=limit)

    def search_messages(
        self,
        *,
        query: str,
        mode: Literal["and", "or"],
        filters: MessageFilter,
        cursor: PageCursor | None,
        limit: int,
    ) -> _Request:
        return self._issue(
            "search",
            query=query,
            mode=mode,
            filters=filters,
            cursor=cursor,
            limit=limit,
        )

    def cancel(self, channel: Literal["list/search"]) -> _Request | None:
        self.cancelled_channels.append(channel)
        return None

    def _issue(self, operation: str, **kwargs: object) -> _Request:
        self._next_request_id += 1
        self.calls.append((operation, kwargs))
        return _Request(self._next_request_id)

    def emit_page(self, request: _Request, page: SearchPage) -> None:
        self.result.emit(
            SimpleNamespace(
                channel="list/search",
                request_id=request.request_id,
                value=page,
            )
        )


def _summary(
    message_id: int,
    *,
    date_sent: datetime | None,
    internal_date: datetime | None = None,
) -> MessageSummary:
    return MessageSummary(
        id=message_id,
        account_id="account-1",
        folder_id=10,
        folder_raw_name="INBOX",
        folder_display_name="受信箱",
        subject=f"件名 {message_id}",
        sender="sender@example.com",
        date_sent=date_sent,
        internal_date=internal_date,
        size_bytes=128,
        has_attachment=False,
        remote_state="present",
        local_state="active",
        thread_key=None,
        imap_flags="\\Seen",
        moved_to_folder_display_name=None,
        failure_class=None,
    )


def test_fetch_more_inserts_rows_only_when_the_async_result_arrives(qtbot: object) -> None:
    del qtbot
    worker = _FakeQueryWorker()
    model = MessageTableModel(cast(MessageQueryWorker, worker), page_size=2)
    request = None

    model.fetchMore()
    request = _Request(worker._next_request_id)
    assert model.rowCount() == 0
    assert model.request_pending

    page = SearchPage((_summary(1, date_sent=datetime(2026, 1, 1, tzinfo=UTC)),), None, True)
    QTimer.singleShot(0, lambda: worker.emit_page(request, page))
    assert _wait_until(lambda: model.rowCount() == 1)
    assert model.items == page.items
    assert model.exhausted


def test_all_pages_match_the_expected_list_with_tied_and_null_dates(qtbot: object) -> None:
    del qtbot
    worker = _FakeQueryWorker()
    model = MessageTableModel(cast(MessageQueryWorker, worker), page_size=2)
    same_date = datetime(2026, 1, 2, tzinfo=UTC)
    expected = (
        _summary(4, date_sent=same_date),
        _summary(3, date_sent=same_date),
        _summary(2, date_sent=None, internal_date=datetime(2026, 1, 1, tzinfo=UTC)),
        _summary(1, date_sent=None),
    )
    first_cursor = PageCursor("2026-01-02T00:00:00Z", 3)
    pages = (
        SearchPage(expected[:2], first_cursor, False),
        SearchPage(expected[2:], None, True),
    )

    for page in pages:
        model.fetchMore()
        request = _Request(worker._next_request_id)
        worker.emit_page(request, page)

    assert model.items == expected
    assert model.next_cursor is None
    assert model.exhausted
    assert worker.calls[1][1]["cursor"] is first_cursor


def test_old_request_result_is_discarded_after_filter_change(qtbot: object) -> None:
    del qtbot
    worker = _FakeQueryWorker()
    model = MessageTableModel(cast(MessageQueryWorker, worker))

    model.fetchMore()
    old_request = _Request(worker._next_request_id)
    model.set_filters(MessageFilter(account_ids=("account-2",)))
    model.fetchMore()
    new_request = _Request(worker._next_request_id)

    stale_page = SearchPage((_summary(1, date_sent=None),), None, True)
    worker.emit_page(old_request, stale_page)
    assert model.rowCount() == 0

    worker.emit_page(new_request, SearchPage((_summary(2, date_sent=None),), None, True))
    assert model.rowCount() == 1
    assert model.items[0].id == 2
    assert worker.cancelled_channels == ["list/search"]


def _wait_until(predicate: object) -> bool:
    """Let the Qt event loop deliver a queued model result in this test."""

    # ``QTimer.singleShot`` delivers synchronously during the next event-loop turn.
    from PySide6.QtWidgets import QApplication

    application = QApplication.instance()
    if application is None:
        return False
    for _ in range(100):
        application.processEvents()
        if callable(predicate) and predicate():
            return True
    return False