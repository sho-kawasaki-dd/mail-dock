from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Any, cast

from mail_dock.domain.errors import StorageDetachedError
from mail_dock.domain.messages import StoredEml
from mail_dock.domain.ports import BaseEmlStorage, BaseManifestWriter, JSONValue
from mail_dock.infrastructure.storage.manifest import ManifestWriter, _encode_event

eml_storage = cast(Any, import_module("mail_dock.infrastructure.storage.eml_storage"))


class InjectedDeviceDetachError(OSError):
    """An OS error that is classified as a detached storage device."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.winerror = 1167


class InjectedSqliteIoError(sqlite3.OperationalError):
    """A SQLite error that is classified as a storage I/O failure."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.sqlite_errorname = "SQLITE_IOERR"


class FaultInjector:
    """Raise a deterministic storage fault on the configured operation call."""

    def __init__(self, operation: str, occurrence: int = 1) -> None:
        if occurrence <= 0:
            raise ValueError("occurrence must be positive")
        self.operation = operation
        self.occurrence = occurrence
        self.calls: dict[str, int] = {}

    def hit(self, operation: str) -> None:
        count = self.calls.get(operation, 0) + 1
        self.calls[operation] = count
        if operation != self.operation or count != self.occurrence:
            return
        if operation == "db.commit":
            raise InjectedSqliteIoError("injected SQLITE_IOERR during commit")
        raise InjectedDeviceDetachError(f"injected storage detach during {operation}")


class FaultInjectingEmlStorage(BaseEmlStorage):
    """Delegate EML operations while allowing deterministic operation faults."""

    def __init__(self, storage: BaseEmlStorage, injector: FaultInjector) -> None:
        self._storage = storage
        self._injector = injector

    def save(self, account_id: str, internal_date: datetime | None, raw: bytes) -> StoredEml:
        self._injector.hit("eml.save")
        return self._storage.save(account_id, internal_date, raw)

    def reuse(self, relative_path: str, expected_hash: str) -> StoredEml | None:
        return self._storage.reuse(relative_path, expected_hash)

    def read(self, relative_path: str) -> bytes:
        return self._storage.read(relative_path)

    def read_verified(self, relative_path: str, expected_hash: str) -> bytes:
        return self._storage.read_verified(relative_path, expected_hash)


class FaultInjectingConnection:
    """Proxy a SQLite connection and inject a classified failure at commit."""

    def __init__(self, connection: sqlite3.Connection, injector: FaultInjector) -> None:
        self._connection = connection
        self._injector = injector

    def commit(self) -> None:
        self._injector.hit("db.commit")
        self._connection.commit()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


class PartialWriteManifest(BaseManifestWriter):
    """Write half of one manifest record, then simulate a device detach."""

    def __init__(self, root: Path, account_id: str, injector: FaultInjector) -> None:
        self._root = root
        self._account_id = account_id
        self._injector = injector
        self._writer = ManifestWriter(root, account_id)
        self._partial_written = False

    @property
    def last_checkpoint_sequence(self) -> int | None:
        return self._writer.last_checkpoint_sequence

    def append(self, event: Mapping[str, JSONValue]) -> None:
        if not self._partial_written and event.get("event") == "fetch":
            timestamp = event.get("timestamp")
            if not isinstance(timestamp, str):
                raise TypeError("test manifest event must have a timestamp")
            normalized = timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp
            parsed_timestamp = datetime.fromisoformat(normalized)
            path = (
                self._root
                / "manifests"
                / "imap"
                / self._account_id
                / f"events-{parsed_timestamp:%Y%m}.jsonl"
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            encoded = _encode_event(event)
            with path.open("ab") as handle:
                handle.write(encoded[: max(1, len(encoded) // 2)])
                handle.flush()
                os.fsync(handle.fileno())
            self._partial_written = True
            try:
                self._injector.hit("manifest.append")
            except InjectedDeviceDetachError as error:
                raise StorageDetachedError(str(error)) from error
        self._writer.append(event)

    def flush_and_sync(self) -> None:
        self._writer.flush_and_sync()

    def checkpoint(self, sequence: int, batch_id: str) -> None:
        self._writer.checkpoint(sequence, batch_id)

    def close(self) -> None:
        self._writer.close()


@contextmanager
def fail_before_eml_fsync(injector: FaultInjector) -> Iterator[None]:
    """Inject a detach immediately before the first EML fsync."""

    eml_os = cast(Any, eml_storage.os)
    original_fsync = eml_os.fsync

    def fsync(file_descriptor: int) -> None:
        injector.hit("eml.fsync")
        original_fsync(file_descriptor)

    eml_os.fsync = fsync
    try:
        yield
    finally:
        eml_os.fsync = original_fsync


@contextmanager
def fail_before_eml_replace(injector: FaultInjector) -> Iterator[None]:
    """Inject a detach immediately before the EML atomic rename."""

    eml_os = cast(Any, eml_storage.os)
    original_replace = eml_os.replace

    def replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        injector.hit("eml.replace")
        original_replace(source, destination)

    eml_os.replace = replace
    try:
        yield
    finally:
        eml_os.replace = original_replace
