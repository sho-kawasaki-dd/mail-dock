"""Preflight and job-resolution use cases for PST imports."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, cast

from mail_dock.domain.errors import (
    InsufficientSpaceError,
    OperationCancelledError,
    SourceChangedError,
    StorageDetachedError,
    UnreadableArchive,
)
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.importer import (
    BaseArchiveImporter,
    ExtractResult,
    ImportOptions,
    SourceFileSnapshot,
)
from mail_dock.domain.messages import ParsedMessage
from mail_dock.domain.ports import (
    BaseEmlStorage,
    BasePstImportStorage,
    BasePstManifestWriter,
    JSONValue,
)
from mail_dock.domain.repository import (
    BasePstImportRepository,
    MessageContents,
    MessageRecord,
)

_HASH_LENGTH = hashlib.sha256().digest_size * 2
_STAGE_SCHEMA_VERSION = 1
_MAX_PARSE_SIZE = 100 * 1024 * 1024
_COMPLETE_STATUSES = frozenset({"completed", "completed_with_errors"})
_INCOMPLETE_STATUSES = frozenset(
    {
        "extracting",
        "ready_to_ingest",
        "ingesting",
        "cancelled_resumable",
        "failed_resumable",
    }
)

class StorageWriteGate(Protocol):
    """Minimal storage-state contract used while a converter is running."""

    def is_write_allowed(self) -> bool: ...


@dataclass(frozen=True)
class StageAResult:
    """Durable inventory produced after a successful readpst extraction."""

    staging_root: os.PathLike[str]
    marker_path: os.PathLike[str]
    total_files: int
    inventory_sha256: str
    static_manifest_sha256: str


@dataclass(frozen=True)
class StageBResult:
    """Outcome of one resumable PST ingestion run."""

    status: str
    processed_count: int
    ingested_count: int
    failed_count: int
    remaining_count: int


@dataclass(frozen=True)
class GenerationVerification:
    """Verified durable state that is safe to expose as the active generation."""

    import_id: int
    import_uuid: str
    item_count: int
    failed_count: int


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False
    ).encode("utf-8")


def _content_hashed_snapshot(payload: dict[str, JSONValue]) -> dict[str, JSONValue]:
    snapshot = dict(payload)
    snapshot.setdefault("schema_version", _STAGE_SCHEMA_VERSION)
    snapshot.pop("content_sha256", None)
    snapshot["content_sha256"] = hashlib.sha256(_canonical_json(snapshot)).hexdigest()
    return snapshot


def read_stage_a_marker(
    marker_path: os.PathLike[str], pst_storage: BasePstImportStorage
) -> dict[str, JSONValue]:
    """Read and validate a Stage A completion marker."""

    payload = dict(pst_storage.read_marker(marker_path))
    required = {
        "schema_version",
        "import_uuid",
        "staging_relative_path",
        "total_files",
        "inventory_sha256",
        "static_manifest_sha256",
        "created_at",
    }
    if not required.issubset(payload):
        missing = sorted(required.difference(payload))
        raise UnreadableArchive(f"Stage A marker is missing fields: {missing}")
    if payload["schema_version"] != _STAGE_SCHEMA_VERSION:
        raise UnreadableArchive("Unsupported Stage A marker schema version")
    if not isinstance(payload["import_uuid"], str):
        raise UnreadableArchive("Stage A marker import_uuid is invalid")
    try:
        parsed_uuid = uuid.UUID(payload["import_uuid"])
    except ValueError as error:
        raise UnreadableArchive("Stage A marker import_uuid is invalid") from error
    if parsed_uuid.version != 4:
        raise UnreadableArchive("Stage A marker import_uuid is not a version 4 UUID")
    if not isinstance(payload["staging_relative_path"], str):
        raise UnreadableArchive("Stage A marker staging path is invalid")
    staging_path = payload["staging_relative_path"].replace("\\", "/")
    if (
        staging_path.startswith(("/", "\\"))
        or (len(staging_path) > 1 and staging_path[1] == ":")
        or ".." in staging_path.split("/")
    ):
        raise UnreadableArchive("Stage A marker staging path must stay below storage root")
    total_files = payload["total_files"]
    if not isinstance(total_files, int) or isinstance(total_files, bool) or total_files < 0:
        raise UnreadableArchive("Stage A marker total_files is invalid")
    for field in ("inventory_sha256", "static_manifest_sha256"):
        value = payload[field]
        if not isinstance(value, str) or len(value) != _HASH_LENGTH:
            raise UnreadableArchive(f"Stage A marker {field} is invalid")
        if any(character not in "0123456789abcdef" for character in value):
            raise UnreadableArchive(f"Stage A marker {field} is invalid")
    if not isinstance(payload["created_at"], str):
        raise UnreadableArchive("Stage A marker created_at is invalid")
    return payload


def _inventory_hash(items: list[dict[str, JSONValue]]) -> str:
    return hashlib.sha256(_canonical_json(items)).hexdigest()


def _static_manifest_hash(
    import_snapshot: dict[str, JSONValue], folders: list[dict[str, JSONValue]]
) -> str:
    import_payload = _content_hashed_snapshot(import_snapshot)
    folders_payload = _content_hashed_snapshot(
        {"schema_version": _STAGE_SCHEMA_VERSION, "folders": cast(JSONValue, folders)}
    )
    encoded = _canonical_json(import_payload) + b"\n" + _canonical_json(folders_payload) + b"\n"
    return hashlib.sha256(encoded).hexdigest()


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _manifest_snapshot(manifest: BasePstManifestWriter) -> Mapping[str, JSONValue]:
    reader = getattr(manifest, "read_import_manifest", None)
    if not callable(reader):
        raise UnreadableArchive("PST manifest cannot read read_import_manifest")
    snapshot = reader()
    if not isinstance(snapshot, Mapping):
        raise UnreadableArchive("PST import manifest is invalid")
    return snapshot


def _verify_folder_snapshot(manifest: BasePstManifestWriter) -> None:
    reader = getattr(manifest, "read_folders_manifest", None)
    if not callable(reader):
        raise UnreadableArchive("PST manifest cannot read read_folders_manifest")
    folders = reader()
    if not isinstance(folders, list):
        raise UnreadableArchive("PST folder manifest is invalid")


def verify_generation(
    repository: BasePstImportRepository,
    manifest: BasePstManifestWriter,
    storage: BaseEmlStorage,
    *,
    import_id: int,
    import_uuid: str,
    allow_superseded: bool = False,
) -> GenerationVerification:
    """Verify the durable EML, manifest, and DB view of one PST generation."""

    record = repository.get_import(import_id)
    if record is None or str(record.get("import_uuid")) != import_uuid:
        raise UnreadableArchive("PST generation does not exist")
    valid_statuses = {"completed", "completed_with_errors"}
    if allow_superseded:
        valid_statuses.add("superseded")
    if record.get("status") not in valid_statuses:
        raise UnreadableArchive("PST generation is not complete")

    snapshot = _manifest_snapshot(manifest)
    if snapshot.get("import_uuid") != import_uuid:
        raise UnreadableArchive("PST import manifest UUID does not match the DB")
    if snapshot.get("account_id") != record.get("account_id"):
        raise UnreadableArchive("PST import manifest account does not match the DB")
    _verify_folder_snapshot(manifest)
    folders = list(repository.list_folders(str(record["account_id"])))
    folder_names = {str(folder.get("raw_name")) for folder in folders}

    events = list(manifest.read_events())
    discovered = _event_by_item(events, "item_discovered")
    saved = _event_by_item(events, "item_saved")
    ready_events = [event for event in events if event.get("event") == "import_ready"]
    if not ready_events:
        raise UnreadableArchive("PST generation has no import_ready event")

    items = list(repository.list_items(import_id))
    if len(discovered) != len(items) or set(discovered) != {
        str(item.get("source_item_key")) for item in items
    }:
        raise UnreadableArchive("PST item manifest and DB inventory differ")
    failed_count = 0
    for item in items:
        source_key = str(item.get("source_item_key"))
        if str(item.get("folder_relative_path")) not in folder_names:
            raise UnreadableArchive(f"PST folder is not registered: {source_key}")
        if item.get("status") not in {"saved", "completed"}:
            raise UnreadableArchive(f"PST item is not saved: {source_key}")
        message_id = item.get("message_row_id")
        saved_event = saved.get(source_key)
        if not isinstance(message_id, int) or saved_event is None:
            raise UnreadableArchive(f"PST item has no durable message: {source_key}")
        if (
            discovered[source_key].get("source_relative_path")
            != item.get("source_relative_path")
            or discovered[source_key].get("folder_relative_path")
            != item.get("folder_relative_path")
            or discovered[source_key].get("source_size_bytes")
            != item.get("source_size_bytes")
            or discovered[source_key].get("source_sha256")
            != item.get("source_sha256")
            or item.get("final_relative_path") != saved_event.get("final_relative_path")
            or item.get("source_sha256") is None
            or saved_event.get("file_hash") is None
            or item.get("final_relative_path") is None
        ):
            raise UnreadableArchive(f"PST item storage metadata differs: {source_key}")
        message = repository.get_message(message_id)
        if message is None or any(
            (
                message.get("account_id") != record.get("account_id"),
                message.get("remote_state") != "no_remote",
                message.get("local_state") not in {"active", "trashed"},
                message.get("relative_path") != saved_event.get("final_relative_path"),
                message.get("file_hash") != saved_event.get("file_hash"),
            )
        ):
            raise UnreadableArchive(f"PST message registration is invalid: {source_key}")
        stored = storage.reuse(
            str(item.get("final_relative_path")), str(saved_event.get("file_hash"))
        )
        if stored is None or stored.size_bytes != saved_event.get("size_bytes"):
            raise UnreadableArchive(f"PST EML is missing or corrupt: {source_key}")
        error_class = item.get("error_class")
        if error_class is not None:
            failed_count += 1
            expected_event = (
                "item_oversize" if error_class == "oversize" else "item_parse_failed"
            )
            if not any(
                event.get("event") == expected_event
                and event.get("source_item_key") == source_key
                for event in events
            ):
                raise UnreadableArchive(f"PST item error is not in the manifest: {source_key}")

    ready = ready_events[-1]
    if ready.get("total_files") != len(items):
        raise UnreadableArchive("PST import_ready count differs from the DB")
    return GenerationVerification(import_id, import_uuid, len(items), failed_count)


def recover_generation_switch(
    repository: BasePstImportRepository,
    manifest: BasePstManifestWriter,
    *,
    import_id: int,
    import_uuid: str,
    replaces_id: int,
) -> None:
    """Restore the old generation when a prepared switch lacks a commit event."""

    events = list(manifest.read_events())
    prepared = any(
        event.get("event") == "generation_switch_prepared" for event in events
    )
    committed = any(
        event.get("event") == "generation_switch_committed" for event in events
    )
    if prepared and not committed:
        repository.rollback_generation_switch(import_id, replaces_id)


def switch_generation(
    repository: BasePstImportRepository,
    manifest: BasePstManifestWriter,
    storage: BaseEmlStorage,
    *,
    import_id: int,
    import_uuid: str,
    replaces_id: int,
    replaces_import_uuid: str,
) -> GenerationVerification:
    """Atomically make a verified re-import visible and retire its predecessor."""

    new_record = repository.get_import(import_id)
    old_record = repository.get_import(replaces_id)
    if (
        new_record is None
        or old_record is None
        or new_record.get("is_active")
        or old_record.get("is_active") != 1
        or new_record.get("replaces_id") != replaces_id
        or old_record.get("import_uuid") != replaces_import_uuid
    ):
        raise UnreadableArchive("PST generations are not an inactive replacement pair")
    verification = verify_generation(
        repository, manifest, storage, import_id=import_id, import_uuid=import_uuid
    )
    manifest.append(
        {
            "event": "generation_switch_prepared",
            "import_uuid": import_uuid,
            "timestamp": _timestamp(),
            "replaces_import_uuid": replaces_import_uuid,
        }
    )
    manifest.flush_and_sync()

    repository.begin_batch()
    try:
        repository.activate_generation(import_id, replaces_id)
        manifest.append(
            {
                "event": "generation_switch_committed",
                "import_uuid": import_uuid,
                "timestamp": _timestamp(),
                "replaces_import_uuid": replaces_import_uuid,
            }
        )
        manifest.flush_and_sync()
        repository.commit_batch()
    except Exception:
        repository.rollback_batch()
        raise

    manifest.append(
        {
            "event": "generation_superseded",
            "import_uuid": import_uuid,
            "timestamp": _timestamp(),
            "superseded_import_uuid": replaces_import_uuid,
        }
    )
    manifest.flush_and_sync()
    return verification


def restore_generation(
    repository: BasePstImportRepository,
    manifest: BasePstManifestWriter,
    storage: BaseEmlStorage,
    *,
    import_id: int,
    import_uuid: str,
    superseded_import_uuid: str,
) -> GenerationVerification:
    """Restore a superseded generation while keeping the switch transactional."""

    verification = verify_generation(
        repository,
        manifest,
        storage,
        import_id=import_id,
        import_uuid=import_uuid,
        allow_superseded=True,
    )
    repository.begin_batch()
    try:
        repository.restore_generation(import_id)
        manifest.append(
            {
                "event": "generation_restored",
                "import_uuid": import_uuid,
                "timestamp": _timestamp(),
                "restored_import_uuid": import_uuid,
                "superseded_import_uuid": superseded_import_uuid,
            }
        )
        manifest.flush_and_sync()
        repository.commit_batch()
    except Exception:
        repository.rollback_batch()
        raise
    return verification


def _ensure_write_allowed(storage_state: StorageWriteGate | None) -> None:
    if storage_state is not None and not storage_state.is_write_allowed():
        raise StorageDetachedError("PST import requires attached storage")


def _remove_staging(
    pst_storage: BasePstImportStorage,
    staging_root: os.PathLike[str],
    storage_state: StorageWriteGate | None,
) -> None:
    _ensure_write_allowed(storage_state)
    pst_storage.remove_staging(staging_root)


def _abandon_stage_a(
    repository: BasePstImportRepository,
    manifest: BasePstManifestWriter,
    import_id: int,
    import_uuid: str,
    staging_root: os.PathLike[str],
    error: Exception,
    storage_state: StorageWriteGate | None,
    pst_storage: BasePstImportStorage,
) -> None:
    _ensure_write_allowed(storage_state)
    reason = "cancelled" if isinstance(error, OperationCancelledError) else "failed"
    manifest.append(
        {
            "event": "import_abandoned",
            "import_uuid": import_uuid,
            "timestamp": _timestamp(),
            "reason": reason,
        }
    )
    manifest.flush_and_sync()
    repository.update_import_status(
        import_id,
        "abandoned",
        staging_path=None,
        finished_at=_timestamp(),
        error_message=str(error),
    )
    _remove_staging(pst_storage, staging_root, storage_state)


def run_stage_a(
    repository: BasePstImportRepository,
    manifest: BasePstManifestWriter,
    extractor: BaseArchiveImporter,
    *,
    import_id: int,
    import_uuid: str,
    account_id: str,
    source: os.PathLike[str],
    storage_root: os.PathLike[str],
    source_snapshot: SourceFileSnapshot,
    readpst_version: str,
    options: ImportOptions,
    cancel: CancelToken | None = None,
    on_progress: Callable[[int], None] | None = None,
    storage_state: StorageWriteGate | None = None,
    pst_storage: BasePstImportStorage,
) -> StageAResult:
    """Extract a PST, durably inventory its staging tree, and publish readiness.

    The caller must create the import row during preflight.  Stage A owns the
    extraction directory and never leaves a partial directory after an
    attached-storage failure, cancellation, or converter error.
    """

    try:
        parsed_uuid = uuid.UUID(import_uuid)
    except (ValueError, AttributeError, TypeError) as error:
        raise ValueError("import_uuid must be a UUID") from error
    if parsed_uuid.version != 4:
        raise ValueError("import_uuid must be a version 4 UUID")
    token = cancel or CancelToken()
    progress = on_progress or (lambda _count: None)
    source_path = pst_storage.normalize_path(source)
    stage_root = pst_storage.staging_root(storage_root, import_uuid)
    import_snapshot: dict[str, JSONValue] = {
        "account_id": account_id,
        "display_name": options.display_name,
        "import_uuid": import_uuid,
        "created_at": _timestamp(),
        "source_filename": pst_storage.source_filename(source_path),
        "source_sha256": source_snapshot.source_sha256,
        "source_size_bytes": source_snapshot.size_bytes,
        "source_mtime": source_snapshot.mtime_ns,
        "source_file_identity": (
            {}
            if source_snapshot.file_identity is None
            else {
                "st_dev": source_snapshot.file_identity[0],
                "st_ino": source_snapshot.file_identity[1],
            }
        ),
        "readpst_version": readpst_version,
        "options": {
            "charset": options.charset,
            "include_deleted": options.include_deleted,
        },
    }

    _ensure_write_allowed(storage_state)
    _remove_staging(pst_storage, stage_root, storage_state)
    pst_storage.create_staging(stage_root)

    try:
        _ensure_write_allowed(storage_state)
        repository.update_import_status(
            import_id,
            "extracting",
            staging_path=str(stage_root),
            error_message=None,
        )
        manifest.write_import_manifest(import_snapshot)
        manifest.flush_and_sync()
        token.raise_if_cancelled()

        def report(count: int) -> None:
            _ensure_write_allowed(storage_state)
            progress(count)

        result: ExtractResult = extractor.extract(
            source_path,
            stage_root,
            options,
            cancel=token,
            on_progress=report,
        )
        if pst_storage.normalize_path(result.staging_root) != stage_root:
            raise UnreadableArchive("Archive extractor returned an unexpected staging directory")
        validate_source_snapshot(
            source_path, source_snapshot, cancel=token, pst_storage=pst_storage
        )
        items, folders = pst_storage.scan_staging(stage_root)
        inventory_sha256 = _inventory_hash(items)
        static_manifest_sha256 = _static_manifest_hash(import_snapshot, folders)

        _ensure_write_allowed(storage_state)
        manifest.write_folders_manifest(cast(list[Mapping[str, JSONValue]], folders))
        event_timestamp = _timestamp()
        for item in items:
            manifest.append(
                {
                    "event": "item_discovered",
                    "import_uuid": import_uuid,
                    "timestamp": event_timestamp,
                    **item,
                }
            )
        manifest.flush_and_sync()

        repository.begin_batch()
        try:
            for item in items:
                repository.upsert_import_item(
                    {
                        "import_id": import_id,
                        **item,
                        "status": "discovered",
                        "attempt_count": 0,
                    }
                )
            repository.commit_batch()
        except Exception:
            repository.rollback_batch()
            raise

        _ensure_write_allowed(storage_state)
        manifest.append(
            {
                "event": "import_ready",
                "import_uuid": import_uuid,
                "timestamp": _timestamp(),
                "total_files": len(items),
                "inventory_sha256": inventory_sha256,
                "static_manifest_sha256": static_manifest_sha256,
            }
        )
        manifest.flush_and_sync()
        relative_stage = pst_storage.relative_path(
            pst_storage.normalize_path(storage_root), stage_root
        )
        _ensure_write_allowed(storage_state)
        marker_path = pst_storage.marker_path(stage_root)
        pst_storage.write_marker(
            marker_path,
            {
                "schema_version": _STAGE_SCHEMA_VERSION,
                "import_uuid": import_uuid,
                "staging_relative_path": relative_stage,
                "total_files": len(items),
                "inventory_sha256": inventory_sha256,
                "static_manifest_sha256": static_manifest_sha256,
                "created_at": _timestamp(),
            },
        )
        read_stage_a_marker(marker_path, pst_storage)
        repository.update_import_status(
            import_id,
            "ready_to_ingest",
            total_files=len(items),
            staging_path=str(stage_root),
            error_message=None,
        )
        return StageAResult(
            staging_root=stage_root,
            marker_path=marker_path,
            total_files=len(items),
            inventory_sha256=inventory_sha256,
            static_manifest_sha256=static_manifest_sha256,
        )
    except StorageDetachedError:
        raise
    except Exception as error:
        _abandon_stage_a(
            repository,
            manifest,
            import_id,
            import_uuid,
            stage_root,
            error,
            storage_state,
            pst_storage,
        )
        raise


def _message_contents(parsed: ParsedMessage) -> MessageContents:
    return {
        "subject_norm": parsed.subject,
        "sender_norm": parsed.sender,
        "body_text": parsed.body_text,
        "attachment_names": "\n".join(
            attachment.filename
            for attachment in parsed.attachments
            if attachment.filename is not None and not attachment.is_inline
        ),
    }


def _stage_b_message(
    item: MessageRecord,
    account_id: str,
    folder_id: object,
    stored_path: str,
    file_hash: str,
    size_bytes: int,
    parsed: ParsedMessage,
    date_sent_iso8601: str | None,
) -> tuple[dict[str, object], MessageContents | None]:
    message = {
        "account_id": account_id,
        "folder_id": folder_id,
        "message_id": parsed.message_id,
        "content_key": parsed.content_key or f"sha256:{file_hash[:32]}",
        "source_item_key": item["source_item_key"],
        "remote_state": "no_remote",
        "local_state": "active",
        "relative_path": stored_path,
        "file_hash": file_hash,
        "subject": parsed.subject,
        "sender": parsed.sender,
        "recipient": parsed.recipient,
        "cc": parsed.cc,
        "date_sent": date_sent_iso8601,
        "size_bytes": size_bytes,
        "has_attachment": int(parsed.has_attachment),
        "in_reply_to": parsed.in_reply_to,
        "references_ids": parsed.references_ids,
        "thread_key": parsed.thread_key,
    }
    contents = None if parsed.parse_error is not None else _message_contents(parsed)
    return message, contents


def _event_by_item(
    events: Sequence[Mapping[str, JSONValue]], event_name: str
) -> dict[str, Mapping[str, JSONValue]]:
    return {
        str(event["source_item_key"]): event
        for event in events
        if event.get("event") == event_name and isinstance(event.get("source_item_key"), str)
    }


def run_stage_b(
    repository: BasePstImportRepository,
    manifest: BasePstManifestWriter,
    storage: BaseEmlStorage,
    *,
    import_id: int,
    import_uuid: str,
    account_id: str,
    staging_root: os.PathLike[str],
    batch_size: int = 100,
    cancel: CancelToken | None = None,
    storage_state: StorageWriteGate | None = None,
    pst_storage: BasePstImportStorage,
) -> StageBResult:
    """Ingest staged EML files in durable, resumable database batches."""

    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    token = cancel or CancelToken()
    root = pst_storage.normalize_path(staging_root)
    marker = read_stage_a_marker(pst_storage.marker_path(root), pst_storage)
    if marker["import_uuid"] != import_uuid:
        raise UnreadableArchive("Stage A marker import_uuid does not match the import")
    items = list(repository.list_incomplete_items(import_id))
    saved_events = _event_by_item(manifest.read_events(), "item_saved")
    parse_failed_events = _event_by_item(manifest.read_events(), "item_parse_failed")
    oversize_events = _event_by_item(manifest.read_events(), "item_oversize")
    processed_count = 0

    _ensure_write_allowed(storage_state)
    repository.update_import_status(import_id, "ingesting", staging_path=str(root))
    try:
        for offset in range(0, len(items), batch_size):
            token.raise_if_cancelled()
            batch = items[offset : offset + batch_size]
            prepared: list[
                tuple[dict[str, object], dict[str, object], MessageContents | None, str]
            ] = []
            for item in batch:
                token.raise_if_cancelled()
                source_key = str(item["source_item_key"])
                source_path = pst_storage.resolve_staging_item(
                    root, str(item["source_relative_path"])
                )
                source_size = int(item["source_size_bytes"])
                oversize = source_size > _MAX_PARSE_SIZE
                staged_message = pst_storage.read_staged_message(source_path, parse=not oversize)
                parsed = staged_message.parsed
                saved_event = saved_events.get(source_key)
                if saved_event is None:
                    _ensure_write_allowed(storage_state)
                    stored = storage.save_from_file(account_id, parsed.date_sent, source_path)
                    if stored.file_hash != str(item["source_sha256"]):
                        raise UnreadableArchive(f"PST staging hash changed: {source_key}")
                    stored_path = stored.relative_path
                    stored_hash = stored.file_hash
                    stored_size = stored.size_bytes
                    saved_event = {
                        "event": "item_saved",
                        "import_uuid": import_uuid,
                        "timestamp": _timestamp(),
                        "source_item_key": source_key,
                        "final_relative_path": stored_path,
                        "file_hash": stored_hash,
                        "size_bytes": stored_size,
                    }
                    _ensure_write_allowed(storage_state)
                    manifest.append(saved_event)
                    saved_events[source_key] = saved_event
                else:
                    stored_path = str(saved_event["final_relative_path"])
                    stored_hash = str(saved_event["file_hash"])
                    stored_size_value = saved_event["size_bytes"]
                    if not isinstance(stored_size_value, int) or isinstance(
                        stored_size_value, bool
                    ):
                        raise UnreadableArchive(f"PST saved event size is invalid: {source_key}")
                    stored_size = stored_size_value

                parse_failed = not oversize and parsed.parse_error is not None
                if parse_failed and source_key not in parse_failed_events:
                    parse_event: dict[str, JSONValue] = {
                        "event": "item_parse_failed",
                        "import_uuid": import_uuid,
                        "timestamp": _timestamp(),
                        "source_item_key": source_key,
                        "error_class": "parse",
                        "error_message": parsed.parse_error or "message parsing failed",
                    }
                    _ensure_write_allowed(storage_state)
                    manifest.append(parse_event)
                    parse_failed_events[source_key] = parse_event
                if oversize and source_key not in oversize_events:
                    oversize_event: dict[str, JSONValue] = {
                        "event": "item_oversize",
                        "import_uuid": import_uuid,
                        "timestamp": _timestamp(),
                        "source_item_key": source_key,
                        "error_class": "oversize",
                        "error_message": "EML exceeds the 100 MiB parse limit",
                        "source_size_bytes": source_size,
                    }
                    _ensure_write_allowed(storage_state)
                    manifest.append(oversize_event)
                    oversize_events[source_key] = oversize_event

                folder_name = str(item.get("folder_relative_path") or ".")
                message, contents = _stage_b_message(
                    item,
                    account_id,
                    0,
                    stored_path,
                    stored_hash,
                    stored_size,
                    parsed,
                    staged_message.date_sent_iso8601,
                )
                item_update: dict[str, object] = {
                    **dict(item),
                    "status": "saved",
                    "final_relative_path": stored_path,
                    "message_row_id": None,
                    "error_class": "oversize" if oversize else "parse" if parse_failed else None,
                    "error_message": (
                        "EML exceeds the 100 MiB parse limit"
                        if oversize
                        else parsed.parse_error if parse_failed else None
                    ),
                    "attempt_count": int(item.get("attempt_count") or 0) + 1,
                }
                prepared.append((item_update, message, contents, folder_name))

            _ensure_write_allowed(storage_state)
            manifest.flush_and_sync()
            repository.begin_batch()
            try:
                for item_update, message, contents, folder_name in prepared:
                    folder_id = repository.upsert_folder(
                        {
                            "account_id": account_id,
                            "raw_name": folder_name,
                            "display_name": (
                                folder_name.rsplit("/", 1)[-1] if folder_name != "." else "."
                            ),
                        }
                    )
                    message["folder_id"] = folder_id
                    message_id = repository.add_message(message, contents)
                    item_update["message_row_id"] = message_id
                    repository.upsert_import_item(item_update)
                batch_items = list(repository.list_items(import_id))
                batch_ingested = sum(
                    1
                    for item in batch_items
                    if item.get("status") in {"saved", "completed"}
                )
                batch_failed = sum(
                    1
                    for item in batch_items
                    if item.get("error_class") in {"parse", "oversize"}
                )
                repository.update_import_status(
                    import_id,
                    "ingesting",
                    total_files=len(batch_items),
                    ingested_count=batch_ingested,
                    failed_count=batch_failed,
                    staging_path=str(root),
                    finished_at=None,
                )
                repository.commit_batch()
            except Exception:
                repository.rollback_batch()
                raise
            processed_count += len(batch)

        all_items = list(repository.list_items(import_id))
        ingested_count = sum(
            1 for item in all_items if item.get("status") in {"saved", "completed"}
        )
        failed_count = sum(
            1 for item in all_items if item.get("error_class") in {"parse", "oversize"}
        )
        remaining_count = len(all_items) - ingested_count
        final_status = (
            "failed_resumable"
            if remaining_count
            else "completed_with_errors" if failed_count else "completed"
        )
        _ensure_write_allowed(storage_state)
        repository.update_import_status(
            import_id,
            final_status,
            total_files=len(all_items),
            ingested_count=ingested_count,
            failed_count=failed_count,
            staging_path=str(root) if remaining_count else None,
            finished_at=_timestamp() if not remaining_count else None,
        )
        if not remaining_count:
            _remove_staging(pst_storage, root, storage_state)
        return StageBResult(
            final_status, processed_count, ingested_count, failed_count, remaining_count
        )
    except OperationCancelledError:
        _ensure_write_allowed(storage_state)
        repository.update_import_status(
            import_id, "cancelled_resumable", staging_path=str(root), finished_at=None
        )
        raise
    except StorageDetachedError:
        raise
    except Exception as error:
        _ensure_write_allowed(storage_state)
        repository.update_import_status(
            import_id, "failed_resumable", staging_path=str(root), error_message=str(error)
        )
        raise


class ImportJobDecision(StrEnum):
    """Actions available when an identical PST was previously imported."""

    RESUME = "resume"
    DISCARD_INCOMPLETE = "discard_incomplete"
    CANCEL = "cancel"
    REIMPORT = "reimport"


class ImportJobAction(StrEnum):
    """Resolution returned to the caller before Stage A starts."""

    NEW = "new"
    NEEDS_INCOMPLETE_DECISION = "needs_incomplete_decision"
    RESUME = "resume"
    DISCARD_INCOMPLETE = "discard_incomplete"
    NEEDS_REIMPORT_DECISION = "needs_reimport_decision"
    CANCEL = "cancel"
    REIMPORT = "reimport"


@dataclass(frozen=True)
class ImportJobResolution:
    """Deterministic result of resolving an existing source hash."""

    action: ImportJobAction
    import_record: MessageRecord | None = None
    incomplete_records: tuple[MessageRecord, ...] = ()
    replaces_id: int | None = None

    @property
    def requires_user_choice(self) -> bool:
        return self.action in {
            ImportJobAction.NEEDS_INCOMPLETE_DECISION,
            ImportJobAction.NEEDS_REIMPORT_DECISION,
        }


@dataclass(frozen=True)
class ImportCapacity:
    """The capacity calculation used to admit a PST import."""

    available_bytes: int
    required_bytes: int
    source_size_bytes: int
    retained_bytes: int


def snapshot_source_file(
    source: os.PathLike[str],
    *,
    pst_storage: BasePstImportStorage,
    cancel: CancelToken | None = None,
    chunk_size: int = 1024 * 1024,
) -> SourceFileSnapshot:
    """Hash a PST through the provider-independent storage port."""

    return pst_storage.snapshot_source_file(source, cancel=cancel, chunk_size=chunk_size)


def validate_source_snapshot(
    source: os.PathLike[str],
    expected: SourceFileSnapshot,
    *,
    pst_storage: BasePstImportStorage,
    cancel: CancelToken | None = None,
) -> SourceFileSnapshot:
    """Re-hash a source and reject any change since the recorded snapshot."""

    actual = snapshot_source_file(source, pst_storage=pst_storage, cancel=cancel)
    if actual != expected:
        raise SourceChangedError(f"PST source changed since it was inspected: {source}")
    return actual


def _validate_source_sha256(source_sha256: str) -> str:
    normalized = source_sha256.casefold()
    if len(normalized) != _HASH_LENGTH or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError("source_sha256 must be a complete SHA-256 digest")
    return normalized


def resolve_import_job(
    repository: BasePstImportRepository,
    source_sha256: str,
    *,
    decision: ImportJobDecision | str | None = None,
) -> ImportJobResolution:
    """Resolve resume/discard or cancel/re-import for a complete source hash.

    This function only returns a decision. Destructive abandonment and new-row
    creation belong to the later import workflow, after the UI has confirmed
    the returned choice.
    """

    source_sha256 = _validate_source_sha256(source_sha256)
    selected_decision = None if decision is None else ImportJobDecision(decision)
    incomplete = tuple(
        sorted(
            (
                record
                for record in repository.find_incomplete_by_source_sha256(source_sha256)
                if record.get("status") in _INCOMPLETE_STATUSES
            ),
            key=lambda record: int(record.get("id", 0)),
        )
    )
    if incomplete:
        if selected_decision is None:
            return ImportJobResolution(
                ImportJobAction.NEEDS_INCOMPLETE_DECISION,
                incomplete_records=incomplete,
            )
        if selected_decision is ImportJobDecision.RESUME:
            return ImportJobResolution(
                ImportJobAction.RESUME,
                import_record=incomplete[0],
                incomplete_records=incomplete,
            )
        if selected_decision is ImportJobDecision.DISCARD_INCOMPLETE:
            return ImportJobResolution(
                ImportJobAction.DISCARD_INCOMPLETE,
                incomplete_records=incomplete,
            )
        raise ValueError("An incomplete PST import accepts resume or discard_incomplete")

    active = repository.find_active_by_source_sha256(source_sha256)
    if active is not None and active.get("status") in _COMPLETE_STATUSES:
        replaces_id = int(active["id"]) if active.get("id") is not None else None
        if selected_decision is None:
            return ImportJobResolution(
                ImportJobAction.NEEDS_REIMPORT_DECISION,
                import_record=active,
                replaces_id=replaces_id,
            )
        if selected_decision is ImportJobDecision.CANCEL:
            return ImportJobResolution(
                ImportJobAction.CANCEL,
                import_record=active,
                replaces_id=replaces_id,
            )
        if selected_decision is ImportJobDecision.REIMPORT:
            return ImportJobResolution(
                ImportJobAction.REIMPORT,
                import_record=active,
                replaces_id=replaces_id,
            )
        raise ValueError("A completed PST import accepts cancel or reimport")

    if selected_decision is not None:
        raise ValueError("No existing PST import requires the selected decision")
    return ImportJobResolution(ImportJobAction.NEW)


def check_import_capacity(
    storage_root: os.PathLike[str],
    source_size_bytes: int,
    *,
    retained_bytes: int = 0,
    check_free_space: Callable[..., object],
    free_space: Callable[..., int],
) -> ImportCapacity:
    """Require the global storage policy and PST-size-based working space."""

    if source_size_bytes < 0 or retained_bytes < 0:
        raise ValueError("source_size_bytes and retained_bytes must be non-negative")
    check_free_space(storage_root)
    available_bytes = free_space(storage_root)
    required_bytes = (source_size_bytes * 5 + 1) // 2 + retained_bytes
    if available_bytes < required_bytes:
        raise InsufficientSpaceError(
            "PST import requires "
            f"{required_bytes} free bytes, but only {available_bytes} are available"
        )
    return ImportCapacity(
        available_bytes=available_bytes,
        required_bytes=required_bytes,
        source_size_bytes=source_size_bytes,
        retained_bytes=retained_bytes,
    )


__all__ = [
    "GenerationVerification",
    "ImportCapacity",
    "ImportJobAction",
    "ImportJobDecision",
    "ImportJobResolution",
    "SourceFileSnapshot",
    "StageBResult",
    "check_import_capacity",
    "recover_generation_switch",
    "resolve_import_job",
    "restore_generation",
    "run_stage_b",
    "snapshot_source_file",
    "switch_generation",
    "validate_source_snapshot",
    "verify_generation",
]