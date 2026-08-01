"""Qt-independent request generation and cancellation state."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Final, Literal

from mail_dock.domain.fetcher import CancelToken

type RequestChannel = Literal["list/search", "detail/open", "count/thread"]


@dataclass(frozen=True)
class RequestHandle:
    """The generation and caller-owned cancellation token for one request."""

    channel: RequestChannel
    request_id: int
    token: CancelToken


class RequestState:
    """Track independent request generations for the query worker.

    Issuing a request cancels only the previous request in the same channel.
    The token remains directly callable by the UI thread while the worker is
    executing a blocking operation.
    """

    CHANNELS: Final[tuple[RequestChannel, ...]] = (
        "list/search",
        "detail/open",
        "count/thread",
    )

    def __init__(self) -> None:
        self._lock = Lock()
        self._next_ids: dict[RequestChannel, int] = {channel: 0 for channel in self.CHANNELS}
        self._current: dict[RequestChannel, RequestHandle | None] = {
            channel: None for channel in self.CHANNELS
        }

    def issue(self, channel: RequestChannel) -> RequestHandle:
        """Cancel the old same-channel request and create the next generation."""

        self._validate_channel(channel)
        with self._lock:
            previous = self._current[channel]
            if previous is not None:
                previous.token.cancel()
            request_id = self._next_ids[channel] + 1
            self._next_ids[channel] = request_id
            handle = RequestHandle(channel, request_id, CancelToken())
            self._current[channel] = handle
            return handle

    def cancel(self, channel: RequestChannel) -> RequestHandle | None:
        """Cancel the current request in one channel without affecting others."""

        self._validate_channel(channel)
        with self._lock:
            current = self._current[channel]
            if current is not None:
                current.token.cancel()
            return current

    def cancel_all(self) -> None:
        """Cancel all current requests, used during application shutdown."""

        with self._lock:
            for current in self._current.values():
                if current is not None:
                    current.token.cancel()

    def is_current(self, channel: RequestChannel, request_id: int) -> bool:
        """Return whether a request is still the newest generation."""

        self._validate_channel(channel)
        with self._lock:
            current = self._current[channel]
            return current is not None and current.request_id == request_id

    def current(self, channel: RequestChannel) -> RequestHandle | None:
        """Return a snapshot of the current request in ``channel``."""

        self._validate_channel(channel)
        with self._lock:
            return self._current[channel]

    def finish(self, handle: RequestHandle) -> None:
        """Release a completed request without invalidating its generation."""

        self._validate_channel(handle.channel)
        with self._lock:
            if self._current[handle.channel] == handle:
                self._current[handle.channel] = None

    @classmethod
    def _validate_channel(cls, channel: RequestChannel) -> None:
        if channel not in cls.CHANNELS:
            raise ValueError(f"unknown request channel: {channel}")