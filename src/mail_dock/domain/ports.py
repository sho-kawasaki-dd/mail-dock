"""Provider-independent ports for credentials, EML storage, and manifests."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime
from pathlib import Path

from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.importer import SourceFileSnapshot, StagedMessage
from mail_dock.domain.messages import AttachmentSavePlan, RenderedMessage, SavedFile, StoredEml

type JSONValue = bool | int | float | str | list[JSONValue] | dict[str, JSONValue] | None

__all__ = [
    "AttachmentSavePlan",
    "BaseCredentialStore",
    "BaseEmlStorage",
    "BaseIntegrityStorage",
    "BaseManifestReader",
    "BaseManifestWriter",
    "BaseMessageRenderer",
    "BasePstImportStorage",
    "BasePstManifestReader",
    "BasePstManifestWriter",
    "BasePurgeStorage",
    "JSONValue",
    "SavedFile",
    "SourceFileSnapshot",
    "StagedMessage",
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

    def save_from_file(
        self, account_id: str, internal_date: datetime | None, source_path: os.PathLike[str]
    ) -> StoredEml:
        """Persist an EML from a file without loading the complete payload in memory."""

        raise NotImplementedError

    @abstractmethod
    def reuse(self, relative_path: str, expected_hash: str) -> StoredEml | None:
        """Reuse a previously stored EML only when its complete hash matches."""

    @abstractmethod
    def read(self, relative_path: str) -> bytes:
        """Read an EML by a storage-relative path after validating its boundary."""

    @abstractmethod
    def read_verified(self, relative_path: str, expected_hash: str) -> bytes:
        """Read an EML once and return it only when its complete hash matches."""


class BaseIntegrityStorage(ABC):
    """Read-only storage operations used by integrity verification."""

    @abstractmethod
    def stat(self, relative_path: str) -> os.stat_result:
        """Return metadata for a storage-relative file path."""

    @abstractmethod
    def iter_chunks(self, relative_path: str, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        """Yield a stored file in bounded-size chunks."""

    @abstractmethod
    def iter_eml_paths(self, account_id: str | None = None) -> Iterator[str]:
        """Yield storage-relative EML paths, optionally limited to an account."""

    @abstractmethod
    def quarantine(self, relative_path: str) -> None:
        """Move a suspect EML below the storage root into quarantine."""


class BasePurgeStorage(ABC):
    """Storage operations that can physically remove a previously stored EML."""

    @abstractmethod
    def exists(self, relative_path: str) -> bool:
        """Return whether a storage-relative file currently exists."""

    @abstractmethod
    def delete(self, relative_path: str) -> None:
        """Delete a storage-relative file; missing files are already purged."""


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

    def close(self) -> None:
        """Release writer resources, if the implementation owns any."""
        return None


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


class BasePstManifestWriter(ABC):
    """Durable manifest port for one PST import generation."""

    @abstractmethod
    def write_import_manifest(self, snapshot: Mapping[str, JSONValue]) -> None:
        """Atomically publish the immutable import metadata snapshot."""

    def write_import_snapshot(self, snapshot: Mapping[str, JSONValue]) -> None:
        """Compatibility spelling for callers that call snapshots explicitly."""

        self.write_import_manifest(snapshot)

    @abstractmethod
    def write_folders_manifest(self, folders: list[Mapping[str, JSONValue]]) -> None:
        """Atomically publish the immutable staging-folder mapping."""

    def write_folders_snapshot(self, folders: list[Mapping[str, JSONValue]]) -> None:
        """Compatibility spelling for callers that call snapshots explicitly."""

        self.write_folders_manifest(folders)

    @abstractmethod
    def append(self, event: Mapping[str, JSONValue]) -> None:
        """Append a validated PST lifecycle or item event without syncing."""
        pass

    def read_events(self) -> Sequence[Mapping[str, JSONValue]]:
        """Return already durable events for idempotent import resumption."""

        return ()

    @abstractmethod
    def flush_and_sync(self) -> None:
        """Flush the JSONL handle and make it durable."""

    def close(self) -> None:
        """Release writer resources, if the implementation owns any."""
        return None

    @property
    def import_uuid(self) -> str | None:
        """Return the generation UUID when the writer is generation-scoped."""

        return None


class BasePstManifestReader(ABC):
    """Read-only port for one PST import generation."""

    @abstractmethod
    def read_import_manifest(self) -> Mapping[str, JSONValue]:
        """Read and verify the immutable import metadata snapshot."""

    def read_import_snapshot(self) -> Mapping[str, JSONValue]:
        """Compatibility spelling for callers that call snapshots explicitly."""

        return self.read_import_manifest()

    @abstractmethod
    def read_folders_manifest(self) -> list[Mapping[str, JSONValue]]:
        """Read and verify the immutable folder mapping snapshot."""

    def read_folders_snapshot(self) -> list[Mapping[str, JSONValue]]:
        """Compatibility spelling for callers that call snapshots explicitly."""

        return self.read_folders_manifest()

    @abstractmethod
    def read_all_events(self) -> Iterator[Mapping[str, JSONValue]]:
        """Yield every valid PST event in append order."""

    def read_incomplete_intents(self) -> Iterator[Mapping[str, JSONValue]]:
        """Yield purge intents without a matching purge completion event."""

        events = list(self.read_all_events())
        completed = {
            (
                event.get("import_uuid"),
                event.get("source_item_key"),
                event.get("relative_path"),
                event.get("file_hash"),
            )
            for event in events
            if event.get("event") == "purged"
        }
        for event in events:
            if event.get("event") != "purge_intent":
                continue
            key = (
                event.get("import_uuid"),
                event.get("source_item_key"),
                event.get("relative_path"),
                event.get("file_hash"),
            )
            if key not in completed:
                yield event


class BasePstImportStorage(ABC):
    """Filesystem and parser operations required by the PST import use case."""

    @abstractmethod
    def normalize_path(self, path: os.PathLike[str]) -> Path:
        """Return a normalized path suitable for the archive importer."""

    @abstractmethod
    def source_filename(self, source: os.PathLike[str]) -> str:
        """Return the display filename for an archive source."""

    @abstractmethod
    def staging_root(self, storage_root: os.PathLike[str], import_uuid: str) -> Path:
        """Return the short, storage-local staging path for an import."""

    @abstractmethod
    def marker_path(self, staging_root: os.PathLike[str]) -> Path:
        """Return the Stage A completion marker path."""

    @abstractmethod
    def create_staging(self, staging_root: os.PathLike[str]) -> None:
        """Create an empty staging directory."""

    @abstractmethod
    def remove_staging(self, staging_root: os.PathLike[str]) -> None:
        """Remove a staging directory recursively."""

    @abstractmethod
    def read_marker(self, marker_path: os.PathLike[str]) -> Mapping[str, JSONValue]:
        """Read a Stage A marker payload."""

    @abstractmethod
    def write_marker(self, marker_path: os.PathLike[str], payload: Mapping[str, JSONValue]) -> None:
        """Durably publish a Stage A marker atomically."""

    @abstractmethod
    def scan_staging(
        self, staging_root: os.PathLike[str]
    ) -> tuple[list[dict[str, JSONValue]], list[dict[str, JSONValue]]]:
        """Build deterministic inventories from a staging tree."""

    @abstractmethod
    def resolve_staging_item(self, staging_root: os.PathLike[str], relative_path: str) -> Path:
        """Resolve and validate one item below a staging root."""

    @abstractmethod
    def read_staged_message(self, source_path: os.PathLike[str], *, parse: bool) -> StagedMessage:
        """Read and parse one staged EML, or return metadata for an oversize file."""

    @abstractmethod
    def snapshot_source_file(
        self,
        source: os.PathLike[str],
        *,
        cancel: CancelToken | None = None,
        chunk_size: int = 1024 * 1024,
    ) -> SourceFileSnapshot:
        """Hash an archive source and capture metadata without modifying it."""

    @abstractmethod
    def relative_path(self, root: os.PathLike[str], child: os.PathLike[str]) -> str:
        """Return a storage-relative POSIX path."""
