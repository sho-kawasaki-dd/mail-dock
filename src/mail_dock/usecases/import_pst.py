"""Preflight and job-resolution use cases for PST imports."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from email import policy
from email.parser import BytesHeaderParser
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast

from mail_dock.domain.errors import (
    InsufficientSpaceError,
    OperationCancelledError,
    SourceChangedError,
    StorageDetachedError,
    UnreadableArchive,
)
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.importer import ExtractResult, ImportOptions
from mail_dock.domain.messages import ParsedMessage
from mail_dock.domain.ports import BaseEmlStorage, BasePstManifestWriter, JSONValue
from mail_dock.domain.repository import (
    BasePstImportRepository,
    MessageContents,
    MessageRecord,
)
from mail_dock.infrastructure.parsing.eml_parser import parse_eml
from mail_dock.infrastructure.parsing.headers import parse_date_header, to_utc_iso8601

_HASH_LENGTH = hashlib.sha256().digest_size * 2
_CHUNK_SIZE = 1024 * 1024
_STAGE_SCHEMA_VERSION = 1
_STAGE_MARKER_NAME = "stageA_done.json"
_MAX_PARSE_SIZE = 100 * 1024 * 1024
_DATE_HEADER_LIMIT = 64 * 1024
_REPARSE_POINT = 0x400
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

_LOGGER = logging.getLogger(__name__)


class StorageWriteGate(Protocol):
    """Minimal storage-state contract used while a converter is running."""

    def is_write_allowed(self) -> bool: ...


class StageAExtractor(Protocol):
    """Extraction-only boundary implemented by the readpst runner."""

    def extract(
        self,
        source: Path,
        staging: Path,
        options: ImportOptions,
        *,
        cancel: CancelToken,
        on_progress: Callable[[int], None],
    ) -> ExtractResult: ...


@dataclass(frozen=True)
class StageAResult:
    """Durable inventory produced after a successful readpst extraction."""

    staging_root: Path
    marker_path: Path
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


def _stage_marker_path(staging_root: Path) -> Path:
    return staging_root / _STAGE_MARKER_NAME


def read_stage_a_marker(marker_path: Path) -> dict[str, JSONValue]:
    """Read and validate a Stage A completion marker."""

    try:
        with marker_path.open("rb") as marker_file:
            payload = json.load(marker_file)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UnreadableArchive(f"Stage A marker cannot be read: {marker_path}") from error
    if not isinstance(payload, dict):
        raise UnreadableArchive("Stage A marker must contain a JSON object")
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
    staging_path = Path(payload["staging_relative_path"])
    if staging_path.is_absolute() or ".." in staging_path.parts:
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


def _write_stage_a_marker(
    marker_path: Path,
    *,
    import_uuid: str,
    staging_relative_path: str,
    total_files: int,
    inventory_sha256: str,
    static_manifest_sha256: str,
) -> None:
    payload: dict[str, JSONValue] = {
        "schema_version": _STAGE_SCHEMA_VERSION,
        "import_uuid": import_uuid,
        "staging_relative_path": staging_relative_path,
        "total_files": total_files,
        "inventory_sha256": inventory_sha256,
        "static_manifest_sha256": static_manifest_sha256,
        "created_at": datetime.now(UTC).isoformat(),
    }
    encoded = _canonical_json(payload) + b"\n"
    temporary_path = marker_path.with_name(f".{marker_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary_path.open("wb") as marker_file:
            marker_file.write(encoded)
            marker_file.flush()
            os.fsync(marker_file.fileno())
        temporary_path.replace(marker_path)
        if os.name != "nt":
            directory_fd = os.open(marker_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except OSError as error:
        raise StorageDetachedError(f"Could not persist Stage A marker: {marker_path}") from error
    finally:
        with suppress(OSError):
            temporary_path.unlink(missing_ok=True)


def _is_reparse_point(metadata: os.stat_result) -> bool:
    return bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT)


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source_file:
            before = os.fstat(source_file.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise UnreadableArchive(f"Staging item is not a regular file: {path}")
            size = 0
            for chunk in iter(lambda: source_file.read(_CHUNK_SIZE), b""):
                digest.update(chunk)
                size += len(chunk)
            after = os.fstat(source_file.fileno())
    except UnreadableArchive:
        raise
    except OSError as error:
        raise UnreadableArchive(f"Could not inspect staging item: {path}") from error
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise UnreadableArchive(f"Staging item changed while it was scanned: {path}")
    return size, digest.hexdigest()


def _scan_staging(
    staging_root: Path,
) -> tuple[list[dict[str, JSONValue]], list[dict[str, JSONValue]]]:
    """Build deterministic item and folder inventories without following links."""

    try:
        root = staging_root.resolve(strict=True)
    except OSError as error:
        raise UnreadableArchive(f"Staging directory cannot be inspected: {staging_root}") from error
    if not root.is_dir():
        raise UnreadableArchive(f"Staging path is not a directory: {staging_root}")

    items: list[dict[str, JSONValue]] = []
    folders: set[str] = set()
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda entry: entry.name)
        except OSError as error:
            raise UnreadableArchive(f"Could not inspect staging directory: {current}") from error
        for entry in entries:
            candidate = Path(entry.path)
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as error:
                _LOGGER.warning("Skipping unreadable PST staging item %s: %s", candidate, error)
                continue
            if entry.is_symlink() or _is_reparse_point(metadata):
                _LOGGER.warning("Skipping link or reparse point in PST staging: %s", candidate)
                continue
            try:
                resolved = candidate.resolve(strict=False)
                resolved.relative_to(root)
            except (OSError, ValueError):
                _LOGGER.warning("Skipping PST staging item outside staging root: %s", candidate)
                continue
            if metadata.st_mode and stat.S_ISDIR(metadata.st_mode):
                pending.append(candidate)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                _LOGGER.warning("Skipping non-regular PST staging item: %s", candidate)
                continue
            relative_path = candidate.relative_to(root).as_posix()
            folder_relative_path = candidate.parent.relative_to(root).as_posix() or "."
            size_bytes, file_hash = _hash_file(candidate)
            items.append(
                {
                    "source_item_key": relative_path,
                    "source_relative_path": relative_path,
                    "folder_relative_path": folder_relative_path,
                    "source_size_bytes": size_bytes,
                    "source_sha256": file_hash,
                }
            )
            folder_path = candidate.parent
            while folder_path != root:
                folders.add(folder_path.relative_to(root).as_posix())
                folder_path = folder_path.parent

    items.sort(key=lambda item: str(item["source_item_key"]))
    folder_records: list[dict[str, JSONValue]] = []
    for folder_name in sorted(folders):
        folder_records.append(
            {"raw_name": folder_name, "display_name": Path(folder_name).name}
        )
    return items, folder_records


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


def _ensure_write_allowed(storage_state: StorageWriteGate | None) -> None:
    if storage_state is not None and not storage_state.is_write_allowed():
        raise StorageDetachedError("PST import requires attached storage")


def _remove_staging(staging_root: Path, storage_state: StorageWriteGate | None) -> None:
    _ensure_write_allowed(storage_state)
    try:
        shutil.rmtree(staging_root, ignore_errors=False)
    except FileNotFoundError:
        return
    except OSError as error:
        raise StorageDetachedError(f"Could not discard PST staging: {staging_root}") from error


def _abandon_stage_a(
    repository: BasePstImportRepository,
    manifest: BasePstManifestWriter,
    import_id: int,
    import_uuid: str,
    staging_root: Path,
    error: Exception,
    storage_state: StorageWriteGate | None,
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
    _remove_staging(staging_root, storage_state)


def run_stage_a(
    repository: BasePstImportRepository,
    manifest: BasePstManifestWriter,
    extractor: StageAExtractor,
    *,
    import_id: int,
    import_uuid: str,
    account_id: str,
    source: Path,
    storage_root: Path,
    source_snapshot: SourceFileSnapshot,
    readpst_version: str,
    options: ImportOptions,
    cancel: CancelToken | None = None,
    on_progress: Callable[[int], None] | None = None,
    storage_state: StorageWriteGate | None = None,
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
    stage_root = (
        Path(storage_root).expanduser().resolve()
        / "tmp"
        / "pstimp"
        / import_uuid.replace("-", "")[:8]
    )
    import_snapshot: dict[str, JSONValue] = {
        "account_id": account_id,
        "display_name": options.display_name,
        "import_uuid": import_uuid,
        "created_at": _timestamp(),
        "source_filename": source.name,
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
    _remove_staging(stage_root, storage_state)
    try:
        stage_root.mkdir(parents=True, exist_ok=False)
    except OSError as error:
        raise StorageDetachedError(f"Could not create PST staging: {stage_root}") from error

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
            source,
            stage_root,
            options,
            cancel=token,
            on_progress=report,
        )
        if result.staging_root.resolve() != stage_root:
            raise UnreadableArchive("Archive extractor returned an unexpected staging directory")
        validate_source_snapshot(source, source_snapshot, cancel=token)
        items, folders = _scan_staging(stage_root)
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
        storage_root_path = Path(storage_root).expanduser().resolve()
        relative_stage = stage_root.relative_to(storage_root_path).as_posix()
        _ensure_write_allowed(storage_state)
        _write_stage_a_marker(
            _stage_marker_path(stage_root),
            import_uuid=import_uuid,
            staging_relative_path=relative_stage,
            total_files=len(items),
            inventory_sha256=inventory_sha256,
            static_manifest_sha256=static_manifest_sha256,
        )
        read_stage_a_marker(_stage_marker_path(stage_root))
        repository.update_import_status(
            import_id,
            "ready_to_ingest",
            total_files=len(items),
            staging_path=str(stage_root),
            error_message=None,
        )
        return StageAResult(
            staging_root=stage_root,
            marker_path=_stage_marker_path(stage_root),
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
        )
        raise


def _stage_item_path(staging_root: Path, relative_path: str) -> Path:
    candidate = (staging_root / Path(relative_path)).resolve()
    try:
        candidate.relative_to(staging_root.resolve())
    except ValueError as error:
        raise UnreadableArchive(f"PST staging item escapes its root: {relative_path}") from error
    if not candidate.is_file():
        raise UnreadableArchive(f"PST staging item is missing: {relative_path}")
    return candidate


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


def _bounded_date(source_path: Path) -> datetime | None:
    try:
        with source_path.open("rb") as source_file:
            prefix = source_file.read(_DATE_HEADER_LIMIT)
    except OSError as error:
        raise UnreadableArchive(f"Could not read PST staging item: {source_path}") from error
    if b"\n\n" not in prefix and b"\r\n\r\n" not in prefix:
        return None
    header = BytesHeaderParser(policy=policy.default).parsebytes(prefix)
    return parse_date_header(header.get("Date"), None)


def _stage_b_message(
    item: MessageRecord,
    account_id: str,
    folder_id: object,
    stored_path: str,
    file_hash: str,
    size_bytes: int,
    parsed: ParsedMessage,
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
        "date_sent": to_utc_iso8601(parsed.date_sent) if parsed.date_sent else None,
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
    staging_root: Path,
    batch_size: int = 100,
    cancel: CancelToken | None = None,
    storage_state: StorageWriteGate | None = None,
) -> StageBResult:
    """Ingest staged EML files in durable, resumable database batches."""

    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    token = cancel or CancelToken()
    root = Path(staging_root).expanduser().resolve()
    marker = read_stage_a_marker(_stage_marker_path(root))
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
                source_path = _stage_item_path(root, str(item["source_relative_path"]))
                source_size = int(item.get("source_size_bytes") or source_path.stat().st_size)
                oversize = source_size > _MAX_PARSE_SIZE
                parsed = ParsedMessage() if oversize else parse_eml(source_path.read_bytes(), None)
                if not oversize:
                    parsed = replace(parsed, date_sent=_bounded_date(source_path))
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
                                Path(folder_name).name if folder_name != "." else "."
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
            _remove_staging(root, storage_state)
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


@dataclass(frozen=True)
class SourceFileSnapshot:
    """Immutable identity captured for a PST before extraction begins."""

    source_sha256: str
    size_bytes: int
    mtime_ns: int
    file_identity: tuple[int, int] | None


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


def _file_identity(metadata: os.stat_result) -> tuple[int, int] | None:
    device = getattr(metadata, "st_dev", None)
    inode = getattr(metadata, "st_ino", None)
    if not isinstance(device, int) or not isinstance(inode, int):
        return None
    return device, inode


def _snapshot_metadata(metadata: os.stat_result) -> tuple[int, int, tuple[int, int] | None]:
    return metadata.st_size, metadata.st_mtime_ns, _file_identity(metadata)


def snapshot_source_file(
    source: Path,
    *,
    cancel: CancelToken | None = None,
    chunk_size: int = _CHUNK_SIZE,
) -> SourceFileSnapshot:
    """Hash a PST through a read-only stream and capture its file metadata."""

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    token = cancel or CancelToken()
    token.raise_if_cancelled()
    digest = hashlib.sha256()
    try:
        with source.open("rb") as source_file:
            initial = os.fstat(source_file.fileno())
            if not stat.S_ISREG(initial.st_mode):
                raise UnreadableArchive(f"PST source is not a regular file: {source}")
            for chunk in iter(lambda: source_file.read(chunk_size), b""):
                token.raise_if_cancelled()
                digest.update(chunk)
            final = os.fstat(source_file.fileno())
    except UnreadableArchive:
        raise
    except OSError as error:
        raise UnreadableArchive(f"Could not read PST source: {source}") from error

    if _snapshot_metadata(initial) != _snapshot_metadata(final):
        raise SourceChangedError(f"PST source changed while it was being read: {source}")
    return SourceFileSnapshot(
        source_sha256=digest.hexdigest(),
        size_bytes=final.st_size,
        mtime_ns=final.st_mtime_ns,
        file_identity=_file_identity(final),
    )


def validate_source_snapshot(
    source: Path,
    expected: SourceFileSnapshot,
    *,
    cancel: CancelToken | None = None,
) -> SourceFileSnapshot:
    """Re-hash a source and reject any change since the recorded snapshot."""

    actual = snapshot_source_file(source, cancel=cancel)
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
    storage_root: Path,
    source_size_bytes: int,
    *,
    retained_bytes: int = 0,
    check_free_space: Callable[[Path], object],
    free_space: Callable[[Path], int],
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
    "ImportCapacity",
    "ImportJobAction",
    "ImportJobDecision",
    "ImportJobResolution",
    "SourceFileSnapshot",
    "StageBResult",
    "check_import_capacity",
    "resolve_import_job",
    "run_stage_b",
    "snapshot_source_file",
    "validate_source_snapshot",
]