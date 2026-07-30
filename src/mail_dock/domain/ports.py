"""Provider-independent ports for credentials, EML storage, and manifests."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from datetime import datetime

from mail_dock.domain.messages import StoredEml

type JSONValue = bool | int | float | str | list[JSONValue] | dict[str, JSONValue] | None


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


class BaseManifestWriter(ABC):
    """Append-only port for durable synchronization events."""

    @abstractmethod
    def append(self, event: Mapping[str, JSONValue]) -> None:
        """Append an event to the manifest without forcing a sync."""

    @abstractmethod
    def flush_and_sync(self) -> None:
        """Flush buffered events and make them durable."""
