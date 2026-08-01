"""Asynchronous, read-only query and message-detail worker."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

from PySide6.QtCore import Signal

from mail_dock.domain.errors import MailDockError, OperationCancelledError
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.ports import BaseEmlStorage
from mail_dock.domain.search import (
    BaseSearchRepository,
    MessageFilter,
    PageCursor,
)
from mail_dock.presentation.threads.request_state import RequestChannel, RequestHandle, RequestState
from mail_dock.presentation.threads.worker import Worker, _Task
from mail_dock.usecases.search_messages import (
    count_messages,
    get_message,
    list_messages,
    list_thread,
    search_messages,
)


class BaseMessageRenderer(Protocol):
    """Minimal renderer boundary needed until the domain C-1 port lands."""

    def render(self, raw: bytes) -> object:
        """Render verified EML bytes into a domain display value."""


SearchMode = Literal["and", "or"]
RepositoryFactory = Callable[[], BaseSearchRepository]
StorageFactory = Callable[[], BaseEmlStorage]
RendererFactory = Callable[[], BaseMessageRenderer]
OpenMessageUseCase = Callable[..., object]


@dataclass(frozen=True)
class QueryResult:
    """A successful result tagged with its independent request generation."""

    channel: RequestChannel
    request_id: int
    value: object


@dataclass(frozen=True)
class QueryFailure:
    """A request failure tagged for UI-side error routing."""

    channel: RequestChannel
    request_id: int
    error: MailDockError


@dataclass(frozen=True)
class QueryCancelled:
    """A cancellation notification tagged for UI-side busy-state handling."""

    channel: RequestChannel
    request_id: int


class QueryWorker(Worker):
    """Run all read operations on one dedicated query thread.

    Repository, storage, and renderer factories are called inside the worker
    thread. This keeps thread-local SQLite connections and file-backed objects
    out of the GUI thread and avoids importing infrastructure implementations.
    """

    request_failed = Signal(object)
    request_cancelled = Signal(object)

    def __init__(
        self,
        search_repository: RepositoryFactory | BaseSearchRepository,
        *,
        storage_factory: StorageFactory | None = None,
        renderer_factory: RendererFactory | None = None,
        open_message_usecase: OpenMessageUseCase | None = None,
        connection_manager: Any | None = None,
    ) -> None:
        super().__init__(connection_manager)
        self._repository_factory = _as_factory(search_repository)
        self._storage_factory = storage_factory
        self._renderer_factory = renderer_factory
        self._open_message_usecase = open_message_usecase
        self._request_state = RequestState()
        self._requests_by_token: dict[CancelToken, RequestHandle] = {}

        self.task_failed.connect(self._on_task_failed)
        self.task_cancelled.connect(self._on_task_cancelled)
        self.task_completed.connect(self._on_task_completed)

    @property
    def request_state(self) -> RequestState:
        """Expose request state for UI controllers and deterministic tests."""

        return self._request_state

    def list_messages(
        self,
        *,
        filters: MessageFilter | None = None,
        cursor: PageCursor | None = None,
        limit: int = 200,
    ) -> RequestHandle:
        """Queue the default active-state message listing."""

        def operation(repository: BaseSearchRepository, token: CancelToken) -> object:
            return list_messages(
                repository,
                filters=filters,
                cursor=cursor,
                limit=limit,
                cancel=token,
            )

        return self._queue("list/search", operation)

    request_list_messages = list_messages

    def search_messages(
        self,
        *,
        query: str,
        mode: SearchMode = "and",
        filters: MessageFilter | None = None,
        cursor: PageCursor | None = None,
        limit: int = 200,
    ) -> RequestHandle:
        """Queue a parsed search request without normalizing in the UI."""

        return self._queue(
            "list/search",
            lambda repository, token: search_messages(
                repository,
                query=query,
                mode=mode,
                filters=filters,
                cursor=cursor,
                limit=limit,
                cancel=token,
            ),
        )

    request_search_messages = search_messages

    def count_messages(
        self,
        *,
        query: str | None = None,
        mode: SearchMode = "and",
        filters: MessageFilter | None = None,
    ) -> RequestHandle:
        """Queue a count request on the independent count/thread channel."""

        return self._queue(
            "count/thread",
            lambda repository, token: count_messages(
                repository,
                query=query,
                mode=mode,
                filters=filters,
                cancel=token,
            ),
        )

    request_count_messages = count_messages

    def list_thread(
        self,
        *,
        thread_key: str,
        filters: MessageFilter | None = None,
    ) -> RequestHandle:
        """Queue a thread listing on the independent count/thread channel."""

        return self._queue(
            "count/thread",
            lambda repository, token: list_thread(
                repository,
                thread_key=thread_key,
                filters=filters,
                cancel=token,
            ),
        )

    request_list_thread = list_thread

    def get_message(self, *, message_id: int) -> RequestHandle:
        """Queue a message-detail lookup."""

        return self._queue(
            "detail/open",
            lambda repository, token: get_message(repository, message_id=message_id),
        )

    request_get_message = get_message

    def open_message(self, *, message_id: int) -> RequestHandle:
        """Queue the renderer-backed message-opening use case."""

        if self._storage_factory is None or self._renderer_factory is None:
            raise RuntimeError("open_message requires storage and renderer factories")
        usecase = self._open_message_usecase or _default_open_message
        storage_factory = self._storage_factory
        renderer_factory = self._renderer_factory
        return self._queue(
            "detail/open",
            lambda repository, token: usecase(
                repository,
                storage_factory(),
                renderer_factory(),
                message_id=message_id,
            ),
        )

    request_open_message = open_message

    def cancel(self, channel: RequestChannel) -> RequestHandle | None:
        """Cancel only the current request in ``channel``."""

        return self._request_state.cancel(channel)

    def stop(self) -> None:
        """Cancel request generations before stopping the shared worker thread."""

        self._request_state.cancel_all()
        try:
            super().stop()
        finally:
            self._requests_by_token.clear()

    def _queue(
        self,
        channel: RequestChannel,
        operation: Callable[[BaseSearchRepository, CancelToken], object],
    ) -> RequestHandle:
        handle = self._request_state.issue(channel)
        self._requests_by_token[handle.token] = handle

        def run() -> QueryResult:
            if not self._request_state.is_current(channel, handle.request_id):
                raise OperationCancelledError("stale query request")
            handle.token.raise_if_cancelled()
            value = operation(self._repository_factory(), handle.token)
            if not self._request_state.is_current(channel, handle.request_id):
                raise OperationCancelledError("stale query request")
            return QueryResult(channel, handle.request_id, value)

        try:
            self.submit(run, handle.token)
        except BaseException:
            self._requests_by_token.pop(handle.token, None)
            self._request_state.finish(handle)
            raise
        return handle

    def _emit_task_result(self, task: _Task, value: object) -> None:
        handle = self._requests_by_token.get(task.token)
        if handle is not None and self._request_state.is_current(handle.channel, handle.request_id):
            super()._emit_task_result(task, value)

    def _emit_task_failed(self, task: _Task, error: MailDockError) -> None:
        handle = self._requests_by_token.get(task.token)
        if handle is not None and self._request_state.is_current(handle.channel, handle.request_id):
            super()._emit_task_failed(task, error)

    def _emit_task_cancelled(self, task: _Task) -> None:
        handle = self._requests_by_token.get(task.token)
        if handle is not None and self._request_state.is_current(handle.channel, handle.request_id):
            super()._emit_task_cancelled(task)

    def _on_task_failed(self, token: object, error: object) -> None:
        if not isinstance(token, CancelToken):
            return
        handle = self._requests_by_token.get(token)
        if handle is None or not isinstance(error, MailDockError):
            return
        if self._request_state.is_current(handle.channel, handle.request_id):
            self.request_failed.emit(QueryFailure(handle.channel, handle.request_id, error))

    def _on_task_cancelled(self, token: object) -> None:
        if not isinstance(token, CancelToken):
            return
        handle = self._requests_by_token.get(token)
        if handle is None:
            return
        if self._request_state.is_current(handle.channel, handle.request_id):
            self.request_cancelled.emit(QueryCancelled(handle.channel, handle.request_id))

    def _on_task_completed(self, token: object) -> None:
        if not isinstance(token, CancelToken):
            return
        handle = self._requests_by_token.pop(token, None)
        if handle is not None and self._request_state.current(handle.channel) == handle:
            self._request_state.finish(handle)


def _as_factory(
    repository: RepositoryFactory | BaseSearchRepository,
) -> RepositoryFactory:
    if callable(repository) and not hasattr(repository, "search_messages"):
        return repository
    fixed_repository = cast(BaseSearchRepository, repository)
    return lambda: fixed_repository


def _default_open_message(
    search_repository: BaseSearchRepository,
    storage: BaseEmlStorage,
    renderer: BaseMessageRenderer,
    *,
    message_id: int,
) -> object:
    """Import the future C-4 use case only when an open request is executed."""

    from importlib import import_module

    module = import_module("mail_dock.usecases.open_message")
    usecase = module.open_message
    return usecase(search_repository, storage, renderer, message_id=message_id)