"""State and request coordination for the message list view."""

from __future__ import annotations

from typing import Literal, Protocol, cast

from PySide6.QtCore import QObject, Signal

from mail_dock.domain.search import MessageFilter, PageCursor
from mail_dock.presentation.errors import user_message

SearchMode = Literal["and", "or"]
ListSearchChannel = Literal["list/search"]


class _RequestHandle(Protocol):
    request_id: int


class _Signal(Protocol):
    def connect(self, slot: object) -> object:
        """Connect a Qt-like signal to a slot."""


class MessageListQueryWorker(Protocol):
    """Worker surface required by :class:`MessageListViewModel`."""

    result: _Signal

    def list_messages(
        self,
        *,
        filters: MessageFilter,
        cursor: PageCursor | None,
        limit: int,
    ) -> _RequestHandle:
        """Queue one listing page."""

    def search_messages(
        self,
        *,
        query: str,
        mode: SearchMode,
        filters: MessageFilter,
        cursor: PageCursor | None,
        limit: int,
    ) -> _RequestHandle:
        """Queue one search page."""

    def cancel(self, channel: ListSearchChannel) -> _RequestHandle | None:
        """Cancel the current list/search request."""


class MessageListViewModel(QObject):
    """Own message-list state and mediate requests to ``QueryWorker``.

    The ViewModel deliberately keeps the query text opaque. Parsing and
    normalization are responsibilities of the search use case running in the
    worker thread. A view can update the search state while the user types and
    call :meth:`execute_search` only when Enter confirms the query.
    """

    filters_changed = Signal(object)
    search_changed = Signal(str, str)
    search_requested = Signal(str, str)
    message_selected = Signal(object)
    selection_changed = Signal(object)
    result_received = Signal(object)
    request_failed = Signal(object)
    request_cancelled = Signal(object)
    search_error_changed = Signal(str)
    slow_path_changed = Signal(bool)
    request_busy_changed = Signal(bool)

    DEFAULT_PAGE_SIZE = 200

    def __init__(
        self,
        worker: MessageListQueryWorker,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._worker = worker
        self._filters = MessageFilter()
        self._query = ""
        self._mode: SearchMode = "and"
        self._selected_message_id: int | None = None
        self._pending_request_id: int | None = None
        self._search_error = ""
        self._has_slow_path = False

        worker.result.connect(self._on_result)
        request_failed = getattr(worker, "request_failed", None)
        if request_failed is not None:
            request_failed.connect(self._on_request_failed)
        request_cancelled = getattr(worker, "request_cancelled", None)
        if request_cancelled is not None:
            request_cancelled.connect(self._on_request_cancelled)
        search_path_detected = getattr(worker, "search_path_detected", None)
        if search_path_detected is not None:
            search_path_detected.connect(self._on_search_path_detected)

    @property
    def filters(self) -> MessageFilter:
        """Return the active structured message filter."""

        return self._filters

    @property
    def query(self) -> str:
        """Return the raw query exactly as supplied by the view."""

        return self._query

    @property
    def mode(self) -> SearchMode:
        """Return the active search combination mode."""

        return self._mode

    @property
    def selected_message_id(self) -> int | None:
        """Return the selected message identifier, if any."""

        return self._selected_message_id

    @property
    def request_pending(self) -> bool:
        """Return whether a list/search request is currently active."""

        return self._pending_request_id is not None

    @property
    def search_error(self) -> str:
        """Return the current inline search error, if any."""

        return self._search_error

    @property
    def has_slow_path(self) -> bool:
        """Return whether the active query uses the LIKE slow path."""

        return self._has_slow_path

    def set_filters(self, filters: MessageFilter) -> None:
        """Set filters, invalidate the old page, and request a fresh page."""

        if filters == self._filters:
            return
        self._cancel_list_search()
        self._filters = filters
        self._clear_search_feedback()
        self.filters_changed.emit(filters)
        self.request_page()

    def set_search_query(self, query: str) -> None:
        """Update the query without executing it.

        This lets a view reflect text edits without triggering a search for
        every keystroke.
        """

        if query == self._query:
            return
        self._query = query
        self._clear_search_error()
        self.search_changed.emit(self._query, self._mode)

    def set_search_mode(self, mode: SearchMode) -> None:
        """Update the search mode without executing a search."""

        self._validate_mode(mode)
        if mode == self._mode:
            return
        self._mode = mode
        self._clear_search_error()
        self.search_changed.emit(self._query, self._mode)

    def execute_search(
        self,
        query: str | None = None,
        mode: SearchMode | None = None,
    ) -> _RequestHandle:
        """Execute the current or supplied query through ``QueryWorker``.

        The caller controls when this method is invoked, so a text field can
        implement Enter-only search confirmation without a debounce timer.
        """

        if query is not None:
            self._query = query
        if mode is not None:
            self._validate_mode(mode)
            self._mode = mode
        self._clear_search_error()
        self._set_slow_path(False)
        self.search_changed.emit(self._query, self._mode)
        self.search_requested.emit(self._query, self._mode)
        return self.request_page()

    search = execute_search

    def clear_search(self) -> _RequestHandle:
        """Clear the query and return to the unsearched message list."""

        return self.execute_search(query="")

    def request_page(
        self,
        cursor: PageCursor | None = None,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> _RequestHandle:
        """Request one page using the current filters and search state."""

        if limit <= 0:
            raise ValueError("limit must be positive")
        self._cancel_list_search()
        if self._query:
            handle = self._worker.search_messages(
                query=self._query,
                mode=self._mode,
                filters=self._filters,
                cursor=cursor,
                limit=limit,
            )
        else:
            handle = self._worker.list_messages(
                filters=self._filters,
                cursor=cursor,
                limit=limit,
            )
        self._pending_request_id = handle.request_id
        self.request_busy_changed.emit(True)
        return handle

    fetch_more = request_page

    def cancel_search(self) -> _RequestHandle | None:
        """Cancel only the list/search request owned by this ViewModel."""

        return self._cancel_list_search()

    def select_message(self, message_id: int | None) -> None:
        """Publish a changed row selection."""

        if message_id is not None and type(message_id) is not int:
            raise TypeError("message_id must be an int or None")
        if message_id == self._selected_message_id:
            return
        self._selected_message_id = message_id
        self.message_selected.emit(message_id)
        self.selection_changed.emit(message_id)

    def _cancel_list_search(self) -> _RequestHandle | None:
        if self._pending_request_id is None:
            return None
        handle = self._worker.cancel("list/search")
        self._pending_request_id = None
        self.request_busy_changed.emit(False)
        return handle

    def _on_result(self, result: object) -> None:
        if getattr(result, "channel", None) != "list/search":
            return
        if getattr(result, "request_id", None) != self._pending_request_id:
            return
        self._pending_request_id = None
        self.request_busy_changed.emit(False)
        value = getattr(result, "value", None)
        self._set_slow_path(bool(getattr(value, "has_slow_path", False)))
        self.result_received.emit(result)

    def _on_request_failed(self, failure: object) -> None:
        if getattr(failure, "channel", None) != "list/search":
            return
        if getattr(failure, "request_id", None) != self._pending_request_id:
            return
        self._pending_request_id = None
        self.request_busy_changed.emit(False)
        error = getattr(failure, "error", failure)
        self._set_search_error(user_message(cast(BaseException, error)))
        self.request_failed.emit(failure)

    def _on_request_cancelled(self, cancelled: object) -> None:
        if getattr(cancelled, "channel", None) != "list/search":
            return
        if getattr(cancelled, "request_id", None) != self._pending_request_id:
            return
        self._pending_request_id = None
        self.request_busy_changed.emit(False)
        self.request_cancelled.emit(cancelled)

    def _on_search_path_detected(self, notice: object) -> None:
        if getattr(notice, "channel", None) != "list/search":
            return
        if getattr(notice, "request_id", None) != self._pending_request_id:
            return
        self._set_slow_path(bool(getattr(notice, "has_slow_path", False)))

    def _clear_search_feedback(self) -> None:
        self._clear_search_error()
        self._set_slow_path(False)

    def _clear_search_error(self) -> None:
        self._set_search_error("")

    def _set_search_error(self, message: str) -> None:
        if message == self._search_error:
            return
        self._search_error = message
        self.search_error_changed.emit(message)

    def _set_slow_path(self, value: bool) -> None:
        if value == self._has_slow_path:
            return
        self._has_slow_path = value
        self.slow_path_changed.emit(value)

    @staticmethod
    def _validate_mode(mode: str) -> None:
        if mode not in ("and", "or"):
            raise ValueError("mode must be 'and' or 'or'")
