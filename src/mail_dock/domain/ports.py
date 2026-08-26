"""Provider-independent ports for credentials, EML storage, and manifests."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from datetime import datetime

from mail_dock.domain.messages import AttachmentSavePlan, RenderedMessage, SavedFile, StoredEml

type JSONValue = bool | int | float | str | list[JSONValue] | dict[str, JSONValue] | None

__all__ = [
    "AttachmentSavePlan",
    "BaseCredentialStore",
    "BaseEmlStorage",
    "BaseManifestReader",
    "BaseManifestWriter",
    "BaseMessageRenderer",
    "JSONValue",
    "SavedFile",
]


class BaseCredentialStore(ABC):
    """Use-case port for credentials kept outside the application database."""

    @abstractmethod
    def set_password(self, account_id: str, password: str) -> None:
        """Store an account password in the configured credential backend."""

    @abstractmethod
    def get_password(self, account_id: str) -> str | None:
        """Return an account password, or ``None`` when it is not stored."""

    @abstractmethod
    def delete_password(self, account_id: str) -> None:
        """Remove an account password from the credential backend."""


class BaseEmlStorage(ABC):
    """Use-case port for atomic EML persistence and integrity-checked reads."""

    @abstractmethod
    def save(self, account_id: str, internal_date: datetime | None, raw: bytes) -> StoredEml:
        """Persist raw EML bytes and return their stored location and hash."""

    @abstractmethod
    def reuse(self, relative_path: str, expected_hash: str) -> StoredEml | None:
        """Reuse a previously stored EML only when its complete hash matches."""

    @abstractmethod
    def read(self, relative_path: str) -> bytes:
        """Read an EML by a storage-relative path after validating its boundary."""

    @abstractmethod
    def read_verified(self, relative_path: str, expected_hash: str) -> bytes:
        """Read an EML once and return it only when its complete hash matches."""


class BaseMessageRenderer(ABC):
    """Use-case port for rendering verified EML bytes for display and attachments."""

    @abstractmethod
    def render(self, raw: bytes) -> RenderedMessage:
        """Render EML bytes, keeping parts in stable MIME walk order.

        Consumers may use a zero-based ``part_index`` into ``parts``. Both
        regular attachments and inline parts are included in that order.
        """


class BaseManifestWriter(ABC):
    """Append-only port for durable synchronization events."""

    @property
    def last_checkpoint_sequence(self) -> int | None:
        """Return the latest durable checkpoint sequence, if known."""
        return None

    @abstractmethod
    def append(self, event: Mapping[str, JSONValue]) -> None:
        """Append an event to the manifest without forcing a sync."""

    @abstractmethod
    def flush_and_sync(self) -> None:
        """Flush buffered events and make them durable."""

    @abstractmethod
    def checkpoint(self, sequence: int, batch_id: str) -> None:
        """Append and durably flush a completed synchronization batch marker."""


class BaseManifestReader(ABC):
    """Read-only port for an account's durable manifest history."""

    @abstractmethod
    def read_all_events(self) -> Iterator[Mapping[str, JSONValue]]:
        """Yield every valid event in manifest order."""

    @abstractmethod
    def read_last_checkpoint(self) -> Mapping[str, JSONValue] | None:
        """Return the latest durable checkpoint, if one exists."""

    @abstractmethod
    def read_events_since_checkpoint(self) -> Iterator[Mapping[str, JSONValue]]:
        """Yield events written after the latest durable checkpoint."""

    @abstractmethod
    def read_incomplete_intents(self) -> Iterator[Mapping[str, JSONValue]]:
        """Yield destructive-operation intents without completion events."""
