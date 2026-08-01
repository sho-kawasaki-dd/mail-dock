"""Asynchronous table model for paginated message summaries."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Literal, Protocol

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    Qt,
)
from PySide6.QtGui import QBrush, QColor, QIcon
from PySide6.QtWidgets import QApplication, QStyle

from mail_dock.domain.search import (
    MessageFilter,
    MessageSummary,
    PageCursor,
    SearchPage,
)
from mail_dock.presentation import strings

SearchMode = Literal["and", "or"]
ListSearchChannel = Literal["list/search"]
_EMPTY_INDEX = QModelIndex()


class _SignalLike(Protocol):
    def connect(self, slot: Callable[[object], None]) -> object:
        """Connect a callback to a worker signal."""


class _RequestHandleLike(Protocol):
    request_id: int


class MessageQueryWorker(Protocol):
    """The small worker surface needed by :class:`MessageTableModel`.

    Keeping this protocol local avoids coupling the model to a worker
    implementation or to infrastructure classes.
    """

    result: _SignalLike

    def list_messages(
        self,
        *,
        filters: MessageFilter,
        cursor: PageCursor | None,
        limit: int,
    ) -> _RequestHandleLike:
        """Queue one listing page."""

    def search_messages(
        self,
        *,
        query: str,
        mode: SearchMode,
        filters: MessageFilter,
        cursor: PageCursor | None,
        limit: int,
    ) -> _RequestHandleLike:
        """Queue one search page."""

    def cancel(self, channel: ListSearchChannel) -> _RequestHandleLike | None:
        """Cancel the current request in the list/search channel."""


class MessageThreadQueryWorker(Protocol):
    """Worker surface needed to load a thread into this model."""

    result: _SignalLike

    def list_thread(
        self,
        *,
        thread_key: str,
        filters: MessageFilter | None = None,
    ) -> _RequestHandleLike:
        """Queue one thread listing."""

    def cancel(self, channel: Literal["count/thread"]) -> _RequestHandleLike | None:
        """Cancel the current thread request."""


_HEADERS = (
    strings.TABLE_HEADER_DATE,
    strings.TABLE_HEADER_ACCOUNT,
    strings.TABLE_HEADER_FOLDER,
    strings.TABLE_HEADER_FROM,
    strings.TABLE_HEADER_SUBJECT,
    strings.TABLE_HEADER_SIZE,
)
_STATUS_COLUMN = 4
_DELETED_BRUSH = QBrush(QColor("#808080"))


def _standard_icon(theme_name: str, fallback: QStyle.StandardPixmap) -> QIcon:
    """Return a themed status icon with a platform-provided fallback."""

    icon = QIcon.fromTheme(theme_name)
    if not icon.isNull():
        return icon
    return QApplication.style().standardIcon(fallback)


def _has_imap_flag(flags: str | None, expected: str) -> bool:
    return expected in (flags or "").split()


def _status_icon(summary: MessageSummary) -> QIcon | None:
    if summary.remote_state == "deleted":
        return _standard_icon("edit-delete", QStyle.StandardPixmap.SP_TrashIcon)
    if not _has_imap_flag(summary.imap_flags, "\\Seen"):
        return _standard_icon(
            "mail-unread", QStyle.StandardPixmap.SP_MessageBoxInformation
        )
    if _has_imap_flag(summary.imap_flags, "\\Flagged"):
        return _standard_icon("starred", QStyle.StandardPixmap.SP_DialogApplyButton)
    return None


class MessageTableModel(QAbstractTableModel):
    """Display ``MessageSummary`` rows while loading pages asynchronously."""

    DEFAULT_PAGE_SIZE = 200

    def __init__(
        self,
        worker: MessageQueryWorker,
        parent: QObject | None = None,
        *,
        filters: MessageFilter | None = None,
        query: str = "",
        mode: SearchMode = "and",
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        super().__init__(parent)
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        if mode not in ("and", "or"):
            raise ValueError("mode must be 'and' or 'or'")

        self._worker = worker
        self._filters = filters or MessageFilter()
        self._query = query
        self._mode = mode
        self._page_size = page_size
        self._items: list[MessageSummary] = []
        self._next_cursor: PageCursor | None = None
        self._exhausted = False
        self._pending_request_id: int | None = None
        self._thread_mode = False
        self._stopped = False

        worker.result.connect(self._on_result)
        request_failed = getattr(worker, "request_failed", None)
        if request_failed is not None:
            request_failed.connect(self._on_request_finished)
        request_cancelled = getattr(worker, "request_cancelled", None)
        if request_cancelled is not None:
            request_cancelled.connect(self._on_request_finished)

    @property
    def items(self) -> tuple[MessageSummary, ...]:
        """Return the currently displayed rows for selection/view-model code."""

        return tuple(self._items)

    @property
    def next_cursor(self) -> PageCursor | None:
        """Return the opaque cursor supplied by the last page."""

        return self._next_cursor

    @property
    def exhausted(self) -> bool:
        """Whether the last page proved that no more rows are available."""

        return self._exhausted

    @property
    def request_pending(self) -> bool:
        """Whether a list/search page is currently in flight."""

        return self._pending_request_id is not None

    @property
    def worker(self) -> MessageQueryWorker:
        """Return the worker used for asynchronous requests."""

        return self._worker

    @property
    def thread_mode(self) -> bool:
        """Whether the rows currently shown are a thread result."""

        return self._thread_mode

    def rowCount(  # noqa: N802
        self,
        parent: QModelIndex | QPersistentModelIndex = _EMPTY_INDEX,
    ) -> int:
        if parent.isValid():
            return 0
        return len(self._items)

    def columnCount(  # noqa: N802
        self,
        parent: QModelIndex | QPersistentModelIndex = _EMPTY_INDEX,
    ) -> int:
        if parent.isValid():
            return 0
        return len(_HEADERS)

    def headerData(  # noqa: N802
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if (
            orientation == Qt.Orientation.Horizontal
            and role == Qt.ItemDataRole.DisplayRole
            and 0 <= section < len(_HEADERS)
        ):
            return _HEADERS[section]
        return super().headerData(section, orientation, role)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if not index.isValid() or not (0 <= index.row() < len(self._items)):
            return None
        summary = self._items[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display_value(summary, index.column())
        if role == Qt.ItemDataRole.DecorationRole and index.column() == _STATUS_COLUMN:
            return _status_icon(summary)
        if role == Qt.ItemDataRole.ForegroundRole:
            if summary.remote_state == "deleted":
                return _DELETED_BRUSH
            return None
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip_value(summary)
        if role == Qt.ItemDataRole.UserRole:
            return summary
        return None

    def canFetchMore(  # noqa: N802
        self,
        parent: QModelIndex | QPersistentModelIndex = _EMPTY_INDEX,
    ) -> bool:
        """Report whether another page may be requested."""

        return (
            not parent.isValid()
            and not self._stopped
            and not self._thread_mode
            and not self._exhausted
            and self._pending_request_id is None
        )

    def stop_loading(self) -> None:
        """Reject new page requests while the owning window is closing."""

        if self._stopped:
            return
        self._stopped = True
        if self._pending_request_id is not None:
            self._worker.cancel("list/search")
        self._pending_request_id = None
        self._exhausted = True

    def reload(self) -> None:
        """Discard the current page and asynchronously load the first page."""

        if self._stopped:
            return
        self._reset_pages()
        self.fetchMore()

    def fetchMore(  # noqa: N802
        self,
        parent: QModelIndex | QPersistentModelIndex = _EMPTY_INDEX,
    ) -> None:
        """Queue a page request; database access stays inside ``QueryWorker``."""

        if not self.canFetchMore(parent):
            return

        if self._query:
            handle = self._worker.search_messages(
                query=self._query,
                mode=self._mode,
                filters=self._filters,
                cursor=self._next_cursor,
                limit=self._page_size,
            )
        else:
            handle = self._worker.list_messages(
                filters=self._filters,
                cursor=self._next_cursor,
                limit=self._page_size,
            )
        self._pending_request_id = handle.request_id

    def set_filters(self, filters: MessageFilter) -> None:
        """Replace filters and discard pages from the previous query."""

        if filters != self._filters:
            self._filters = filters
            self._reset_pages()

    def show_thread(self, items: tuple[MessageSummary, ...]) -> None:
        """Replace the paginated rows with one already loaded thread.

        The list/search request is deliberately discarded locally instead of
        being cancelled here. Its independent worker channel may still be in
        use by another controller, and stale results are ignored while the
        model is in thread mode.
        """

        self.beginResetModel()
        self._items = list(items)
        self._next_cursor = None
        self._exhausted = True
        self._pending_request_id = None
        self._thread_mode = True
        self.endResetModel()

    def clear_thread(self) -> None:
        """Leave thread mode and prepare the model for a fresh first page."""

        if not self._thread_mode:
            return
        self.beginResetModel()
        self._items.clear()
        self._next_cursor = None
        self._exhausted = False
        self._pending_request_id = None
        self._thread_mode = False
        self.endResetModel()

    def set_search(self, query: str, mode: SearchMode = "and") -> None:
        """Replace the raw query and mode without normalizing in the model."""

        if mode not in ("and", "or"):
            raise ValueError("mode must be 'and' or 'or'")
        if query != self._query or mode != self._mode:
            self._query = query
            self._mode = mode
            self._reset_pages()

    def set_query(self, query: str) -> None:
        """Replace the raw query while preserving the search mode."""

        self.set_search(query, self._mode)

    def _reset_pages(self) -> None:
        if self._pending_request_id is not None:
            self._worker.cancel("list/search")
        self.beginResetModel()
        self._items.clear()
        self._next_cursor = None
        self._exhausted = False
        self._thread_mode = False
        self._pending_request_id = None
        self.endResetModel()

    def _on_result(self, result: object) -> None:
        if getattr(result, "channel", None) != "list/search":
            return
        if self._thread_mode:
            return
        request_id = getattr(result, "request_id", None)
        if request_id != self._pending_request_id:
            return
        page = getattr(result, "value", None)
        if not isinstance(page, SearchPage):
            self._pending_request_id = None
            return

        self._pending_request_id = None
        new_items = tuple(page.items)
        if new_items:
            first = len(self._items)
            last = first + len(new_items) - 1
            self.beginInsertRows(QModelIndex(), first, last)
            self._items.extend(new_items)
            self.endInsertRows()
        self._next_cursor = page.next_cursor
        self._exhausted = page.exhausted

    def _on_request_finished(self, result: object) -> None:
        if getattr(result, "channel", None) != "list/search":
            return
        if getattr(result, "request_id", None) == self._pending_request_id:
            self._pending_request_id = None

    @staticmethod
    def _display_value(summary: MessageSummary, column: int) -> str:
        values = (
            _format_datetime(summary.date_sent or summary.internal_date),
            summary.account_id,
            summary.folder_display_name,
            summary.sender,
            (
                strings.STATUS_LOCAL_PURGED
                if summary.local_state == "purged"
                else (
                    f"{summary.subject} [{strings.STATUS_OVERSIZE}]"
                    if summary.failure_class == "oversize"
                    else summary.subject
                )
            ),
            _format_size(summary.size_bytes),
        )
        return values[column] if 0 <= column < len(values) else ""

    @staticmethod
    def _tooltip_value(summary: MessageSummary) -> str | None:
        if summary.remote_state == "moved" and summary.moved_to_folder_display_name:
            return strings.STATUS_REMOTE_MOVED.format(
                folder=summary.moved_to_folder_display_name
            )
        if summary.failure_class == "oversize":
            return strings.STATUS_OVERSIZE
        if not _has_imap_flag(summary.imap_flags, "\\Seen"):
            return strings.TOOLTIP_UNREAD
        if _has_imap_flag(summary.imap_flags, "\\Flagged"):
            return strings.STATUS_FLAGGED
        if summary.local_state == "purged":
            return strings.STATUS_LOCAL_PURGED
        return None


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.strftime("%Y-%m-%d %H:%M")


def _format_size(size_bytes: int | None) -> str:
    if size_bytes is None:
        return ""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"