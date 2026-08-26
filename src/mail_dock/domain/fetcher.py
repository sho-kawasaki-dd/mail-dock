"""Provider-neutral contracts for remote mail fetching."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from threading import Event

from mail_dock.domain.errors import OperationCancelledError


@dataclass(frozen=True)
class RemoteFolder:
    """A remote folder, retaining both the protocol and display names."""

    raw_name: str
    display_name: str
    uidvalidity: int | None = None
    special_use: frozenset[str] = frozenset()
    delimiter: str | None = None


@dataclass(frozen=True)
class RemoteMessageRef:
    """Metadata that can be obtained before downloading a complete message."""

    uid: int
    message_id: str | None = None
    internal_date: datetime | None = None
    size_bytes: int | None = None
    flags: tuple[str, ...] = ()


class CancelToken:
    """A small cancellation boundary shared by synchronous and future UI workers."""

    def __init__(self, event: Event | None = None) -> None:
        self._event = event if event is not None else Event()

    @property
    def event(self) -> Event:
        """Expose the event for wait-aware retry implementations."""

        return self._event

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise OperationCancelledError("operation cancelled")


class BaseMailFetcher(ABC):
    """Provider boundary for live, incrementally synchronized mail sources.

    Implementations must not contain retry or backoff logic. Retry handling is
    centralized in the use-case layer so every provider follows the same
    policy.
    """

    # Do not combine this contract with BaseArchiveImporter (Phase 4.5): a
    # live fetcher and a one-time archive import have different resume
    # semantics and cancellation granularity.

    @abstractmethod
    def connect(self) -> None:
        """Open and authenticate the provider connection."""

    @abstractmethod
    def disconnect(self) -> None:
        """Close the provider connection."""

    def __enter__(self) -> BaseMailFetcher:
        self.connect()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.disconnect()

    @abstractmethod
    def list_folders(self) -> list[RemoteFolder]:
        """List folders without exposing provider-specific response objects."""

    @abstractmethod
    def find_trash_folder(self) -> RemoteFolder | None:
        """Return the resolved remote trash folder, or ``None`` if unknown."""

    @abstractmethod
    def supports_uid_expunge(self) -> bool:
        """Return whether UID EXPUNGE is available for safe targeted deletion."""

    @abstractmethod
    def select_folder(self, raw_name: str) -> int:
        """Select a folder and return its current UIDVALIDITY."""

    @abstractmethod
    def iter_message_refs(
        self,
        raw_name: str,
        *,
        min_uid: int = 1,
        max_uid: int | None = None,
        descending: bool = True,
        cancel: CancelToken | None = None,
    ) -> Iterator[RemoteMessageRef]:
        """Yield message metadata lazily in the requested UID order."""

    @abstractmethod
    def iter_flags(
        self,
        raw_name: str,
        uids: Iterable[int],
        *,
        cancel: CancelToken | None = None,
    ) -> Iterator[RemoteMessageRef]:
        """Yield FLAGS-only metadata for the requested UIDs."""

    @abstractmethod
    def iter_flags_since(
        self,
        raw_name: str,
        modseq: int,
        *,
        cancel: CancelToken | None = None,
    ) -> Iterator[RemoteMessageRef]:
        """Yield FLAGS changed since ``modseq`` using CONDSTORE."""

    @abstractmethod
    def get_highest_modseq(self) -> int | None:
        """Return the selected folder's HIGHESTMODSEQ, if available."""

    @abstractmethod
    def get_max_uid(self, raw_name: str) -> int:
        """Return the largest UID in a folder, or zero for an empty folder."""

    @abstractmethod
    def list_existing_uids(self, raw_name: str) -> set[int]:
        """Return the current UID set for deletion detection."""

    @abstractmethod
    def download_eml_bytes(self, raw_name: str, uid: int) -> bytes:
        """Download one complete EML without changing its remote seen state."""

    @abstractmethod
    def download_eml_headers(self, raw_name: str, uid: int) -> bytes:
        """Download only the headers for an oversized message."""

    @abstractmethod
    def delete_remote_message(self, raw_name: str, uid: int, *, mode: str = "trash") -> None:
        """Delete or move one remote message according to the provider policy."""
