"""Local trash and durable EML purge use cases."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from mail_dock.domain.errors import StorageDetachedError
from mail_dock.domain.ports import (
    BaseManifestReader,
    BaseManifestWriter,
    BasePurgeStorage,
    JSONValue,
)
from mail_dock.domain.repository import BaseMessageRepository, MessageRecord

_LOGGER = logging.getLogger(__name__)


class StorageWriteGate(Protocol):
    """Minimal storage-state contract needed by local purge."""

    def is_write_allowed(self) -> bool: ...


@dataclass(frozen=True)
class TrashResult:
    """Messages changed by a local trash or restore operation."""

    trashed_ids: tuple[Any, ...] = ()
    restored_ids: tuple[Any, ...] = ()
    skipped_ids: tuple[Any, ...] = ()

    @property
    def trashed_count(self) -> int:
        return len(self.trashed_ids)

    @property
    def restored_count(self) -> int:
        return len(self.restored_ids)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped_ids)


@dataclass(frozen=True)
class PurgeResult:
    """Summary of a purge run, including shared-file decisions."""

    purged_ids: tuple[Any, ...] = ()
    skipped_ids: tuple[Any, ...] = ()
    physically_deleted_paths: tuple[str, ...] = ()
    shared_paths: tuple[str, ...] = ()
    total_size_bytes: int = 0

    @property
    def purged_count(self) -> int:
        return len(self.purged_ids)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped_ids)

    @property
    def physical_delete_count(self) -> int:
        return len(self.physically_deleted_paths)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _record_id(record: Mapping[str, Any]) -> Any | None:
    return record.get("id")


def _records(repo: BaseMessageRepository, message_ids: Iterable[Any]) -> Iterable[MessageRecord]:
    for message_id in message_ids:
        record = repo.get_message(message_id)
        if record is not None:
            yield record


def move_to_trash(
    repo: BaseMessageRepository,
    *,
    message_ids: Iterable[Any],
    now: datetime,
) -> TrashResult:
    """Mark active messages as trashed without touching their EML files."""

    trashed_ids: list[Any] = []
    skipped_ids: list[Any] = []
    trashed_at = _timestamp(now)
    for message_id in message_ids:
        record = repo.get_message(message_id)
        if record is None or record.get("local_state", "active") == "purged":
            skipped_ids.append(message_id)
            continue
        if record.get("local_state") == "trashed":
            continue
        repo.set_local_state(message_id, "trashed", trashed_at)
        trashed_ids.append(message_id)
    return TrashResult(trashed_ids=tuple(trashed_ids), skipped_ids=tuple(skipped_ids))


def restore_from_trash(
    repo: BaseMessageRepository,
    *,
    message_ids: Iterable[Any],
) -> TrashResult:
    """Restore trashed messages to the active local state."""

    restored_ids: list[Any] = []
    skipped_ids: list[Any] = []
    for message_id in message_ids:
        record = repo.get_message(message_id)
        if record is None or record.get("local_state") != "trashed":
            skipped_ids.append(message_id)
            continue
        repo.set_local_state(message_id, "active", None)
        restored_ids.append(message_id)
    return TrashResult(restored_ids=tuple(restored_ids), skipped_ids=tuple(skipped_ids))


def list_purge_candidates(
    repo: BaseMessageRepository,
    *,
    now: datetime,
    grace_days: int,
) -> Sequence[MessageRecord]:
    """Return trashed messages whose grace period has elapsed."""

    if grace_days < 0:
        raise ValueError("grace_days must be non-negative")
    cutoff = now.astimezone(UTC) if now.tzinfo is not None else now.replace(tzinfo=UTC)
    cutoff -= timedelta(days=grace_days)
    return repo.list_trashed(older_than=cutoff.isoformat())


def _purge_event(
    event: str,
    record: Mapping[str, Any],
    *,
    relative_path: str,
    file_hash: str,
    timestamp: str,
    shared_reference_count: int,
    physical_delete: bool,
) -> dict[str, JSONValue]:
    account_id = record.get("account_id")
    source_item_key = record.get("source_item_key")
    if not isinstance(account_id, str) or not isinstance(source_item_key, str):
        raise ValueError("purge requires account_id and source_item_key")
    return {
        "event": event,
        "account_id": account_id,
        "source_item_key": source_item_key,
        "relative_path": relative_path,
        "file_hash": file_hash,
        "timestamp": timestamp,
        "shared_reference_count": shared_reference_count,
        "physical_delete": physical_delete,
    }


def _audit_entry(
    record: Mapping[str, Any], timestamp: str, physical_delete: bool
) -> dict[str, Any]:
    return {
        "occurred_at": timestamp,
        "operation": "local_purge",
        "account_id": record.get("account_id"),
        "message_id": record.get("id"),
        "subject": record.get("subject"),
        "size_bytes": record.get("size_bytes"),
        "detail": f"physical_delete={str(physical_delete).lower()}",
    }


def _audit_exists(repo: BaseMessageRepository, record: Mapping[str, Any]) -> bool:
    message_id = record.get("id")
    account_id = record.get("account_id")
    for audit in repo.list_audit_log(2**31 - 1, 0):
        if (
            audit.get("operation") == "local_purge"
            and audit.get("message_id") == message_id
            and audit.get("account_id") == account_id
        ):
            return True
    return False


def _finish_purge_database(
    repo: BaseMessageRepository,
    record: Mapping[str, Any],
    *,
    physical_delete: bool,
) -> None:
    message_id = _record_id(record)
    if message_id is None:
        raise ValueError("purge requires a message id")
    repo.begin_batch()
    repo.delete_message_contents(message_id)
    repo.update_message_storage(message_id, None, None)
    repo.set_local_state(message_id, "purged", None)
    if not _audit_exists(repo, record):
        repo.record_audit(_audit_entry(record, _timestamp(datetime.now(UTC)), physical_delete))
    repo.commit_batch()


def _complete_purge(
    repo: BaseMessageRepository,
    storage: BasePurgeStorage,
    manifest: BaseManifestWriter,
    record: Mapping[str, Any],
    *,
    timestamp: str,
    intended_physical_delete: bool | None = None,
    write_intent: bool = True,
) -> tuple[bool, bool, str | None]:
    message_id = _record_id(record)
    relative_path = record.get("relative_path")
    file_hash = record.get("file_hash")
    account_id = record.get("account_id")
    if (
        message_id is None
        or not isinstance(relative_path, str)
        or not relative_path
        or not isinstance(file_hash, str)
        or not file_hash
        or not isinstance(account_id, str)
    ):
        return False, False, None

    shared_reference_count = repo.count_path_references(account_id, relative_path, message_id)
    physical_delete = shared_reference_count == 0
    if intended_physical_delete is not None:
        physical_delete = physical_delete and intended_physical_delete

    if write_intent:
        manifest.append(
            _purge_event(
                "purge_intent",
                record,
                relative_path=relative_path,
                file_hash=file_hash,
                timestamp=timestamp,
                shared_reference_count=shared_reference_count,
                physical_delete=physical_delete,
            )
        )
        manifest.flush_and_sync()

    deleted = False
    if physical_delete and storage.exists(relative_path):
        storage.delete(relative_path)
        deleted = True

    manifest.append(
        _purge_event(
            "purged",
            record,
            relative_path=relative_path,
            file_hash=file_hash,
            timestamp=timestamp,
            shared_reference_count=shared_reference_count,
            physical_delete=physical_delete,
        )
    )
    manifest.flush_and_sync()

    _finish_purge_database(repo, record, physical_delete=physical_delete)
    return True, deleted, None if shared_reference_count == 0 else relative_path


def purge(
    repo: BaseMessageRepository,
    storage: BasePurgeStorage,
    manifest: BaseManifestWriter,
    *,
    message_ids: Iterable[Any],
    storage_state: StorageWriteGate,
) -> PurgeResult:
    """Durably purge trashed messages while preserving shared EML files."""

    if not storage_state.is_write_allowed():
        raise StorageDetachedError("Local purge requires attached storage")

    purged_ids: list[Any] = []
    skipped_ids: list[Any] = []
    physically_deleted_paths: list[str] = []
    shared_paths: list[str] = []
    timestamp = _timestamp(datetime.now(UTC))
    records = tuple(_records(repo, message_ids))
    total_size_bytes = sum(
        int(record["size_bytes"])
        for record in records
        if isinstance(record.get("size_bytes"), int) and record.get("size_bytes", 0) >= 0
    )
    _LOGGER.info(
        "Starting local purge: count=%d total_size_bytes=%d",
        len(records),
        total_size_bytes,
    )

    for record in records:
        message_id = _record_id(record)
        if message_id is None or record.get("local_state") != "trashed":
            if message_id is not None:
                skipped_ids.append(message_id)
            continue
        path = record.get("relative_path")
        completed, deleted, shared_path = _complete_purge(
            repo, storage, manifest, record, timestamp=timestamp
        )
        if not completed:
            skipped_ids.append(message_id)
            continue
        purged_ids.append(message_id)
        if deleted and isinstance(path, str):
            physically_deleted_paths.append(path)
        if shared_path is not None:
            shared_paths.append(shared_path)

    _LOGGER.info(
        "Local purge complete: count=%d physical_delete_count=%d",
        len(purged_ids),
        len(physically_deleted_paths),
    )
    return PurgeResult(
        purged_ids=tuple(purged_ids),
        skipped_ids=tuple(skipped_ids),
        physically_deleted_paths=tuple(physically_deleted_paths),
        shared_paths=tuple(shared_paths),
        total_size_bytes=total_size_bytes,
    )


def recover_incomplete_purges(
    repo: BaseMessageRepository,
    storage: BasePurgeStorage,
    manifest_reader: BaseManifestReader,
    *,
    storage_state: StorageWriteGate,
) -> None:
    """Resume purge intents that have no durable ``purged`` event."""

    if not storage_state.is_write_allowed():
        raise StorageDetachedError("Recovering local purges requires attached storage")

    records = tuple(repo.list_stored_messages())
    records_by_key = {
        (record.get("account_id"), record.get("source_item_key")): record for record in records
    }
    events = tuple(manifest_reader.read_all_events())
    completed_events = {
        (event.get("account_id"), event.get("source_item_key")): event
        for event in events
        if event.get("event") == "purged"
    }
    for intent in manifest_reader.read_incomplete_intents():
        if intent.get("event") != "purge_intent":
            continue
        key = (intent.get("account_id"), intent.get("source_item_key"))
        record = records_by_key.get(key)
        if record is None:
            _LOGGER.warning("Cannot recover purge without a database record: key=%s", key)
            continue
        timestamp = intent.get("timestamp")
        relative_path = intent.get("relative_path")
        file_hash = intent.get("file_hash")
        if not isinstance(timestamp, str) or not isinstance(relative_path, str):
            continue
        if not isinstance(file_hash, str):
            continue
        if record.get("relative_path") not in {None, relative_path} or record.get(
            "file_hash"
        ) not in {None, file_hash}:
            _LOGGER.warning("Cannot recover purge with changed message metadata: key=%s", key)
            continue
        recovery_record = {**record, "relative_path": relative_path, "file_hash": file_hash}
        intent_physical_delete = intent.get("physical_delete")
        if not isinstance(intent_physical_delete, bool):
            intent_physical_delete = None
        _complete_purge(
            repo,
            storage,
            _writable_manifest(manifest_reader),
            recovery_record,
            timestamp=timestamp,
            intended_physical_delete=intent_physical_delete,
            write_intent=False,
        )

    for completed in completed_events.values():
        key = (completed.get("account_id"), completed.get("source_item_key"))
        record = records_by_key.get(key)
        if record is None or record.get("local_state") == "purged":
            continue
        physical_delete = completed.get("physical_delete")
        if isinstance(physical_delete, bool):
            _finish_purge_database(repo, record, physical_delete=physical_delete)


def _writable_manifest(manifest_reader: BaseManifestReader) -> BaseManifestWriter:
    writer = getattr(manifest_reader, "writer", manifest_reader)
    if not isinstance(writer, BaseManifestWriter):
        raise TypeError("manifest_reader must also expose a writable manifest for recovery")
    return writer
