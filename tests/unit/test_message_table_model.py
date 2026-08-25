from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Literal, cast

import pytest
from PySide6.QtCore import QModelIndex, QObject, Qt, Signal
from PySide6.QtGui import QBrush

from mail_dock.domain.search import MessageFilter, MessageSummary, PageCursor, SearchPage
from mail_dock.presentation import strings
from mail_dock.presentation.models.message_table_model import (
    MessageQueryWorker,
    MessageTableModel,
)

pytestmark = pytest.mark.gui


@dataclass(frozen=True)
class Request:
    request_id: int


class FakeQueryWorker(QObject):
    result = Signal(object)
    request_failed = Signal(object)
    request_cancelled = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._next_id = 0
        self.cancelled_channels: list[str] = []

    def list_messages(
        self,
        *,
        filters: MessageFilter,
        cursor: PageCursor | None,
        limit: int,
    ) -> Request:
        return self._issue(
            "list",
            {"filters": filters, "cursor": cursor, "limit": limit},
        )

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
            {
                "query": query,
                "mode": mode,
                "filters": filters,
                "cursor": cursor,
                "limit": limit,
            },
        )

    def cancel(self, channel: Literal["list/search"]) -> Request | None:
        self.cancelled_channels.append(channel)
        return None

    def _issue(self, operation: str, kwargs: dict[str, object]) -> Request:
        self._next_id += 1
        self.calls.append((operation, kwargs))
        return Request(self._next_id)

    def emit_page(self, request_id: int, page: SearchPage) -> None:
        self.result.emit(SimpleNamespace(channel="list/search", request_id=request_id, value=page))


def _summary(message_id: int = 1) -> MessageSummary:
    return MessageSummary(
        id=message_id,
        account_id="account-1",
        folder_id=10,
        folder_raw_name="INBOX",
        folder_display_name="受信箱",
        subject="件名",
        sender="sender@example.com",
        date_sent=datetime(2026, 1, 2, 3, 4, tzinfo=UTC),
        internal_date=None,
        size_bytes=2048,
        has_attachment=False,
        remote_state="present",
        local_state="active",
        thread_key=None,
        imap_flags="\\Seen",
        moved_to_folder_display_name=None,
        failure_class=None,
        flags_seen_at=None,
    )


def test_fetch_more_forwards_opaque_cursor_and_inserts_rows(qtbot: object) -> None:
    del qtbot
    worker = FakeQueryWorker()
    model = MessageTableModel(cast(MessageQueryWorker, worker))
    cursor = PageCursor("2026-01-02T03:04:00+00:00", 1)

    assert model.canFetchMore(QModelIndex())
    model.fetchMore(QModelIndex())
    assert not model.canFetchMore(QModelIndex())
    assert worker.calls == [("list", {"filters": MessageFilter(), "cursor": None, "limit": 200})]

    worker.emit_page(1, SearchPage((_summary(),), cursor, False))

    assert model.rowCount() == 1
    assert model.next_cursor is cursor
    assert not model.exhausted
    assert model.canFetchMore(QModelIndex())

    model.fetchMore(QModelIndex())
    assert worker.calls[-1] == (
        "list",
        {"filters": MessageFilter(), "cursor": cursor, "limit": 200},
    )


def test_stale_page_is_discarded_when_search_changes(qtbot: object) -> None:
    del qtbot
    worker = FakeQueryWorker()
    model = MessageTableModel(cast(MessageQueryWorker, worker))
    model.fetchMore(QModelIndex())

    model.set_search("invoice", mode="or")
    assert model.rowCount() == 0
    assert worker.cancelled_channels == ["list/search"]

    worker.emit_page(1, SearchPage((_summary(),), None, True))
    assert model.rowCount() == 0
    assert model.canFetchMore(QModelIndex())

    model.fetchMore(QModelIndex())
    assert worker.calls[-1] == (
        "search",
        {
            "query": "invoice",
            "mode": "or",
            "filters": MessageFilter(),
            "cursor": None,
            "limit": 200,
        },
    )


def test_filter_change_resets_rows_and_cursor(qtbot: object) -> None:
    del qtbot
    worker = FakeQueryWorker()
    model = MessageTableModel(cast(MessageQueryWorker, worker))
    model.fetchMore(QModelIndex())
    worker.emit_page(1, SearchPage((_summary(),), PageCursor("key", 1), True))
    assert model.rowCount() == 1

    model.set_filters(MessageFilter(account_ids=("account-2",)))

    assert model.rowCount() == 0
    assert model.next_cursor is None
    assert not model.exhausted
    assert model.canFetchMore()


def test_columns_render_summary_values(qtbot: object) -> None:
    del qtbot
    worker = FakeQueryWorker()
    model = MessageTableModel(cast(MessageQueryWorker, worker))
    model.fetchMore()
    worker.emit_page(1, SearchPage((_summary(),), None, True))

    headers = [
        model.headerData(column, Qt.Orientation.Horizontal)
        for column in range(model.columnCount(QModelIndex()))
    ]
    values = [
        model.data(model.index(0, column)) for column in range(model.columnCount(QModelIndex()))
    ]

    expected_date_value = _summary().date_sent
    assert expected_date_value is not None
    expected_date = expected_date_value.astimezone().strftime("%Y-%m-%d %H:%M")
    assert headers == ["日付", "アカウント", "フォルダ", "差出人", "件名", "サイズ"]
    assert values == [
        expected_date,
        "account-1",
        "受信箱",
        "sender@example.com",
        "件名",
        "2.0 KB",
    ]


def _model_with_summary(summary: MessageSummary) -> MessageTableModel:
    worker = FakeQueryWorker()
    model = MessageTableModel(cast(MessageQueryWorker, worker))
    model.fetchMore()
    worker.emit_page(1, SearchPage((summary,), None, True))
    return model


def test_status_roles_render_from_summary(qtbot: object) -> None:
    del qtbot
    flagged = _model_with_summary(replace(_summary(), imap_flags="\\Seen \\Flagged"))
    index = flagged.index(0, 4)
    assert flagged.data(index, Qt.ItemDataRole.DecorationRole) is not None
    assert flagged.data(index, Qt.ItemDataRole.ToolTipRole) == strings.TOOLTIP_IMAP_FLAGS_UNKNOWN

    unread = _model_with_summary(replace(_summary(), imap_flags="\\Flagged"))
    assert unread.data(index, Qt.ItemDataRole.ToolTipRole) == strings.TOOLTIP_UNREAD_UNKNOWN

    deleted = _model_with_summary(replace(_summary(), remote_state="deleted", local_state="purged"))
    foreground = deleted.data(index, Qt.ItemDataRole.ForegroundRole)
    assert isinstance(foreground, QBrush)
    assert foreground.color().name() == "#808080"
    assert deleted.data(index, Qt.ItemDataRole.DisplayRole) == strings.STATUS_LOCAL_PURGED
    assert deleted.data(index, Qt.ItemDataRole.DecorationRole) is not None


def test_status_tooltip_includes_local_snapshot_time(qtbot: object) -> None:
    del qtbot
    seen_at = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    model = _model_with_summary(
        replace(_summary(), imap_flags="\\Seen \\Flagged", flags_seen_at=seen_at)
    )

    assert model.data(model.index(0, 4), Qt.ItemDataRole.ToolTipRole) == (
        strings.TOOLTIP_IMAP_FLAGS.format(seen_at=seen_at.astimezone().strftime("%Y-%m-%d %H:%M"))
    )


def test_status_display_and_tooltips_cover_moved_and_oversize(qtbot: object) -> None:
    del qtbot
    moved = _model_with_summary(
        replace(_summary(), remote_state="moved", moved_to_folder_display_name="アーカイブ")
    )
    assert moved.data(moved.index(0, 0), Qt.ItemDataRole.ToolTipRole) == (
        strings.STATUS_REMOTE_MOVED.format(folder="アーカイブ")
    )

    oversize = _model_with_summary(replace(_summary(), failure_class="oversize"))
    assert oversize.data(oversize.index(0, 4), Qt.ItemDataRole.DisplayRole) == (
        f"件名 [{strings.STATUS_OVERSIZE}]"
    )
    assert oversize.data(oversize.index(0, 0), Qt.ItemDataRole.ToolTipRole) == (
        strings.STATUS_OVERSIZE
    )
