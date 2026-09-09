"""Durable manifests for PST import generations.

PST manifests deliberately use a separate implementation from the IMAP
manifest.  They share the CRC-protected JSONL wire format, but their event
schema and lifecycle are different and must not alter IMAP recovery rules.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import uuid
import zlib
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, cast

from mail_dock.domain.errors import ManifestCorruptError
from mail_dock.domain.ports import (
    BasePstManifestReader,
    BasePstManifestWriter,
    JSONValue,
)
from mail_dock.infrastructure.storage.detach import storage_io

__all__ = [
    "PstManifestReader",
    "PstManifestWriter",
    "read_events",
    "repair_tail",
]

_SCHEMA_VERSION = 1
_IMPORT_FILENAME = "import.json"
_FOLDERS_FILENAME = "folders.json"
_ITEMS_FILENAME = "items.jsonl"
_CONTENT_HASH_FIELD = "content_sha256"
_CRC_SUFFIX = re.compile(rb"\|CRC32:([0-9a-fA-F]{8})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_EVENTS = frozenset(
    {
        "item_discovered",
        "item_saved",
        "item_parse_failed",
        "item_oversize",
        "item_reparsed",
        "import_ready",
        "purge_intent",
        "purged",
        "generation_switch_prepared",
        "generation_switch_committed",
        "generation_restored",
        "generation_superseded",
        "import_abandoned",
    }
)
_ITEM_EVENTS = frozenset(
    {"item_discovered", "item_saved", "item_parse_failed", "item_oversize", "item_reparsed"}
)
_IMPORT_REQUIRED = frozenset(
    {
        "account_id",
        "display_name",
        "import_uuid",
        "created_at",
        "source_filename",
        "source_sha256",
        "source_size_bytes",
        "source_mtime",
        "source_file_identity",
        "readpst_version",
        "options",
    }
)
_FOLDER_REQUIRED = frozenset({"raw_name", "display_name"})


def _is_json_value(value: object) -> bool:
    if value is None or isinstance(value, (bool, int, float, str)):
        return True
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_value(item) for key, item in value.items())
    return False


def _validate_import_uuid(import_uuid: str) -> None:
    try:
        parsed = uuid.UUID(import_uuid)
    except (ValueError, AttributeError, TypeError) as error:
        raise ValueError("import_uuid must be a UUID") from error
    if parsed.version != 4:
        raise ValueError("import_uuid must be a version 4 UUID")


def _require_text(payload: Mapping[str, JSONValue], field: str, context: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{context} {field} must be a non-empty string")
    return value


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("PST manifest timestamp must be an ISO-8601 string")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        timestamp = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError("PST manifest timestamp is invalid") from error
    if timestamp.tzinfo is None:
        raise ValueError("PST manifest timestamp must include a timezone")
    return timestamp.astimezone(UTC)


def _validate_sha256(value: object, field: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"PST manifest {field} must be a lowercase SHA-256 hex string")


def _canonical_json(payload: Mapping[str, JSONValue]) -> bytes:
    return json.dumps(
        dict(payload), ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False
    ).encode("utf-8")


def _with_content_hash(payload: Mapping[str, JSONValue]) -> dict[str, JSONValue]:
    content = dict(payload)
    content.pop(_CONTENT_HASH_FIELD, None)
    content.setdefault("schema_version", _SCHEMA_VERSION)
    content[_CONTENT_HASH_FIELD] = hashlib.sha256(_canonical_json(content)).hexdigest()
    return content


def _validate_import_snapshot(snapshot: Mapping[str, JSONValue]) -> dict[str, JSONValue]:
    payload = dict(snapshot)
    if payload.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError("unsupported PST import manifest schema version")
    missing = _IMPORT_REQUIRED.difference(payload)
    if missing:
        raise ValueError(f"import manifest is missing required fields: {sorted(missing)}")
    _validate_import_uuid(_require_text(payload, "import_uuid", "import manifest"))
    _require_text(payload, "account_id", "import manifest")
    _require_text(payload, "display_name", "import manifest")
    _require_text(payload, "source_filename", "import manifest")
    _require_text(payload, "readpst_version", "import manifest")
    _parse_timestamp(payload["created_at"])
    _validate_sha256(payload["source_sha256"], "source_sha256")
    size = payload["source_size_bytes"]
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise ValueError("import manifest source_size_bytes must be a non-negative integer")
    if not isinstance(payload["source_file_identity"], dict):
        raise ValueError("import manifest source_file_identity must be an object")
    if not isinstance(payload["options"], dict):
        raise ValueError("import manifest options must be an object")
    _validate_sha256(payload.get(_CONTENT_HASH_FIELD), _CONTENT_HASH_FIELD)
    expected = _with_content_hash(
        {key: value for key, value in payload.items() if key != _CONTENT_HASH_FIELD}
    )
    if payload[_CONTENT_HASH_FIELD] != expected[_CONTENT_HASH_FIELD]:
        raise ValueError("import manifest content_sha256 does not match its contents")
    return payload


def _validate_folders_snapshot(snapshot: Mapping[str, JSONValue]) -> dict[str, JSONValue]:
    payload = dict(snapshot)
    if payload.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError("unsupported PST folder manifest schema version")
    folders = payload.get("folders")
    if not isinstance(folders, list):
        raise ValueError("folder manifest folders must be an array")
    seen: set[str] = set()
    for folder in folders:
        if not isinstance(folder, dict):
            raise ValueError("folder manifest entries must be objects")
        missing = _FOLDER_REQUIRED.difference(folder)
        if missing:
            raise ValueError(f"folder manifest entry is missing required fields: {sorted(missing)}")
        raw_name = folder["raw_name"]
        display_name = folder["display_name"]
        if not isinstance(raw_name, str) or not raw_name:
            raise ValueError("folder manifest raw_name must be a non-empty string")
        if not isinstance(display_name, str) or not display_name:
            raise ValueError("folder manifest display_name must be a non-empty string")
        if raw_name in seen:
            raise ValueError(f"folder manifest raw_name is duplicated: {raw_name}")
        seen.add(raw_name)
        if "original_name_unresolved" in folder and not isinstance(
            folder["original_name_unresolved"], bool
        ):
            raise TypeError("folder manifest original_name_unresolved must be a boolean")
    _validate_sha256(payload.get(_CONTENT_HASH_FIELD), _CONTENT_HASH_FIELD)
    expected = _with_content_hash(
        {key: value for key, value in payload.items() if key != _CONTENT_HASH_FIELD}
    )
    if payload[_CONTENT_HASH_FIELD] != expected[_CONTENT_HASH_FIELD]:
        raise ValueError("folder manifest content_sha256 does not match its contents")
    return payload


def _validate_event(event: Mapping[str, JSONValue]) -> dict[str, JSONValue]:
    payload = dict(event)
    if not _is_json_value(payload):
        raise TypeError("PST manifest events must contain JSON-compatible values")
    event_name = payload.get("event")
    if not isinstance(event_name, str) or event_name not in _EVENTS:
        raise ValueError(f"unsupported PST manifest event: {event_name!r}")
    import_uuid = _require_text(payload, "import_uuid", event_name)
    _validate_import_uuid(import_uuid)
    _parse_timestamp(payload.get("timestamp"))

    if event_name == "item_discovered":
        required = {
            "source_item_key",
            "source_relative_path",
            "folder_relative_path",
            "source_size_bytes",
            "source_sha256",
        }
        _require_fields(payload, required, event_name)
        _require_item_key(payload, event_name)
        _require_text(payload, "source_relative_path", event_name)
        _require_text(payload, "folder_relative_path", event_name)
        _require_non_negative_int(payload, "source_size_bytes", event_name)
        _validate_sha256(payload["source_sha256"], "source_sha256")
    elif event_name in {"item_saved", "item_reparsed"}:
        required = {"source_item_key", "final_relative_path", "file_hash", "size_bytes"}
        _require_fields(payload, required, event_name)
        _require_item_key(payload, event_name)
        _require_text(payload, "final_relative_path", event_name)
        _validate_sha256(payload["file_hash"], "file_hash")
        _require_non_negative_int(payload, "size_bytes", event_name)
    elif event_name in {"item_parse_failed", "item_oversize"}:
        required = {"source_item_key", "error_class", "error_message"}
        _require_fields(payload, required, event_name)
        _require_item_key(payload, event_name)
        _require_text(payload, "error_class", event_name)
        if not isinstance(payload["error_message"], str):
            raise TypeError(f"{event_name} error_message must be a string")
        if event_name == "item_oversize":
            _require_non_negative_int(payload, "source_size_bytes", event_name)
    elif event_name == "import_ready":
        _require_fields(
            payload,
            {"total_files", "inventory_sha256", "static_manifest_sha256"},
            event_name,
        )
        _require_non_negative_int(payload, "total_files", event_name)
        _validate_sha256(payload["inventory_sha256"], "inventory_sha256")
        _validate_sha256(payload["static_manifest_sha256"], "static_manifest_sha256")
    elif event_name in {"purge_intent", "purged"}:
        _require_fields(
            payload,
            {
                "source_item_key",
                "relative_path",
                "file_hash",
                "shared_reference_count",
                "physical_delete",
            },
            event_name,
        )
        _require_item_key(payload, event_name)
        _require_text(payload, "relative_path", event_name)
        _validate_sha256(payload["file_hash"], "file_hash")
        _require_non_negative_int(payload, "shared_reference_count", event_name)
        if not isinstance(payload["physical_delete"], bool):
            raise TypeError(f"{event_name} physical_delete must be a boolean")
    elif event_name in {"generation_switch_prepared", "generation_switch_committed"}:
        replaces_import_uuid = _require_text(payload, "replaces_import_uuid", event_name)
        _validate_import_uuid(replaces_import_uuid)
    elif event_name == "generation_superseded":
        superseded_import_uuid = _require_text(payload, "superseded_import_uuid", event_name)
        _validate_import_uuid(superseded_import_uuid)
    elif event_name == "generation_restored":
        restored_import_uuid = _require_text(payload, "restored_import_uuid", event_name)
        superseded_import_uuid = _require_text(payload, "superseded_import_uuid", event_name)
        _validate_import_uuid(restored_import_uuid)
        _validate_import_uuid(superseded_import_uuid)
    elif event_name == "import_abandoned":
        _require_text(payload, "reason", event_name)
    if "account_id" in payload:
        _require_text(payload, "account_id", event_name)
    return payload


def _require_fields(payload: Mapping[str, JSONValue], fields: set[str], event_name: str) -> None:
    missing = fields.difference(payload)
    if missing:
        raise ValueError(f"{event_name} event is missing required fields: {sorted(missing)}")


def _require_item_key(payload: Mapping[str, JSONValue], event_name: str) -> None:
    _require_text(payload, "source_item_key", event_name)


def _require_non_negative_int(
    payload: Mapping[str, JSONValue], field: str, event_name: str
) -> None:
    value = payload[field]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TypeError(f"{event_name} {field} must be a non-negative integer")


def _encode_event(event: Mapping[str, JSONValue]) -> bytes:
    payload = _validate_event(event)
    encoded = _canonical_json(payload)
    checksum = zlib.crc32(encoded) & 0xFFFFFFFF
    return encoded + f"|CRC32:{checksum:08x}".encode("ascii") + b"\n"


def _parse_line(line: bytes) -> Mapping[str, JSONValue]:
    if not line.endswith(b"\n"):
        raise ManifestCorruptError("PST manifest record is missing its trailing newline")
    content = line[:-1]
    match = _CRC_SUFFIX.search(content)
    if match is None:
        raise ManifestCorruptError("PST manifest record has no valid CRC32 suffix")
    payload = content[: match.start()]
    if zlib.crc32(payload) & 0xFFFFFFFF != int(match.group(1), 16):
        raise ManifestCorruptError("PST manifest record CRC32 does not match its payload")
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManifestCorruptError("PST manifest record contains invalid JSON") from error
    if not isinstance(decoded, dict) or not _is_json_value(decoded):
        raise ManifestCorruptError("PST manifest record must be a JSON object")
    try:
        return _validate_event(cast(Mapping[str, JSONValue], decoded))
    except (TypeError, ValueError) as error:
        raise ManifestCorruptError(
            "PST manifest record does not satisfy the event schema"
        ) from error


def _fsync_directory(path: Path) -> None:
    if sys.platform == "win32":
        return
    directory_fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _atomic_write_json(path: Path, payload: Mapping[str, JSONValue], root: Path) -> None:
    temporary_dir = root / "tmp" / f"pst-manifest-{uuid.uuid4().hex}"
    temporary_dir.mkdir(parents=True, exist_ok=False)
    temporary_path = temporary_dir / path.name
    try:
        encoded = _canonical_json(payload) + b"\n"
        with storage_io(), temporary_path.open("wb") as temporary_file:
            temporary_file.write(encoded)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            existing = _read_snapshot(path, path.name == _IMPORT_FILENAME)
            if existing == payload:
                return
            raise ValueError(f"PST manifest snapshot is immutable: {path.name}")
        with storage_io():
            os.replace(temporary_path, path)  # noqa: PTH105
        _fsync_directory(path.parent)
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
            temporary_dir.rmdir()
        except OSError:
            pass


def _read_snapshot(path: Path, import_snapshot: bool) -> dict[str, JSONValue]:
    try:
        with storage_io(), path.open("rb") as snapshot_file:
            decoded = json.load(snapshot_file)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManifestCorruptError("PST manifest snapshot cannot be read") from error
    if not isinstance(decoded, dict) or not _is_json_value(decoded):
        raise ManifestCorruptError("PST manifest snapshot must be a JSON object")
    try:
        return (
            _validate_import_snapshot(cast(Mapping[str, JSONValue], decoded))
            if import_snapshot
            else _validate_folders_snapshot(cast(Mapping[str, JSONValue], decoded))
        )
    except (TypeError, ValueError) as error:
        raise ManifestCorruptError("PST manifest snapshot does not satisfy its schema") from error


def _truncate(path: Path, size: int) -> None:
    with storage_io(), path.open("r+b") as manifest_file:
        manifest_file.truncate(size)
        manifest_file.flush()
        os.fsync(manifest_file.fileno())


def read_events(path: Path) -> Iterator[Mapping[str, JSONValue]]:
    """Yield valid events and detach a malformed final record from ``path``."""

    def iterator() -> Iterator[Mapping[str, JSONValue]]:
        offset = 0
        truncate_at: int | None = None
        events: list[Mapping[str, JSONValue]] = []
        with storage_io(), path.open("rb") as manifest_file:
            line = manifest_file.readline()
            while line:
                next_line = manifest_file.readline()
                try:
                    event = _parse_line(line)
                except ManifestCorruptError:
                    if next_line:
                        raise
                    truncate_at = offset
                    break
                try:
                    _validate_transition(event, events)
                except (TypeError, ValueError) as error:
                    if next_line:
                        raise ManifestCorruptError(
                            "PST manifest event sequence is invalid"
                        ) from error
                    truncate_at = offset
                    break
                events.append(event)
                yield event
                offset += len(line)
                line = next_line
        if truncate_at is not None:
            _truncate(path, truncate_at)

    return iterator()


def _event_key(event: Mapping[str, JSONValue]) -> tuple[object, ...]:
    event_name = cast(str, event["event"])
    import_uuid = event["import_uuid"]
    if event_name in _ITEM_EVENTS | {"purge_intent", "purged"}:
        return (
            event_name,
            import_uuid,
            event.get("source_item_key"),
            event.get("relative_path"),
            event.get("file_hash"),
        )
    return (event_name, import_uuid, event.get("replaces_import_uuid"), event.get("reason"))


def _validate_transition(
    event: Mapping[str, JSONValue], events: Sequence[Mapping[str, JSONValue]]
) -> bool:
    key = _event_key(event)
    for previous in events:
        if _event_key(previous) == key:
            comparable = {field: value for field, value in previous.items() if field != "timestamp"}
            current = {field: value for field, value in event.items() if field != "timestamp"}
            if comparable == current:
                return False
            raise ValueError("PST manifest contains a conflicting duplicate idempotency key")
    event_name = cast(str, event["event"])
    import_uuid = event["import_uuid"]
    if event_name in {"item_saved", "item_parse_failed", "item_oversize"}:
        source_item_key = event["source_item_key"]
        if not any(
            previous["import_uuid"] == import_uuid
            and previous.get("source_item_key") == source_item_key
            and previous["event"] == "item_discovered"
            for previous in events
        ):
            raise ValueError(f"{event_name} requires item_discovered first")
    elif event_name == "item_reparsed":
        source_item_key = event["source_item_key"]
        if not any(
            previous["import_uuid"] == import_uuid
            and previous.get("source_item_key") == source_item_key
            and previous["event"] in {"item_parse_failed", "item_oversize"}
            for previous in events
        ):
            raise ValueError("item_reparsed requires an earlier parse failure")
    elif event_name == "purged":
        if not any(
            previous["event"] == "purge_intent"
            and _event_key(previous)[1:] == _event_key(event)[1:]
            for previous in events
        ):
            raise ValueError("purged requires a matching purge_intent")
    elif event_name == "generation_switch_committed":
        if not any(
            previous["event"] == "generation_switch_prepared"
            and previous["import_uuid"] == import_uuid
            and previous.get("replaces_import_uuid") == event.get("replaces_import_uuid")
            for previous in events
        ):
            raise ValueError("generation_switch_committed requires generation_switch_prepared")
    elif event_name == "generation_superseded":
        if not any(
            previous["event"] == "generation_switch_committed"
            and previous["import_uuid"] == import_uuid
            for previous in events
        ):
            raise ValueError("generation_superseded requires generation_switch_committed")
    return True


class PstManifestWriter(BasePstManifestWriter):
    """Write one immutable PST snapshot pair and its append-only event log."""

    def __init__(self, root: Path, import_uuid: str) -> None:
        _validate_import_uuid(import_uuid)
        self._root = root
        self._import_uuid = import_uuid
        self._directory = root / "manifests" / "pst" / import_uuid
        self._handle: BinaryIO | None = None
        self._events = list(read_events(self._directory / _ITEMS_FILENAME)) if (
            self._directory / _ITEMS_FILENAME
        ).is_file() else []

    def _write_snapshot(self, filename: str, payload: Mapping[str, JSONValue]) -> None:
        if filename == _IMPORT_FILENAME:
            validated = _validate_import_snapshot(_with_content_hash(payload))
        else:
            validated = _validate_folders_snapshot(
                _with_content_hash({"folders": list(cast(Sequence[JSONValue], payload["folders"]))})
            )
        _atomic_write_json(self._directory / filename, validated, self._root)

    def write_import_manifest(self, snapshot: Mapping[str, JSONValue]) -> None:
        payload = dict(snapshot)
        payload.setdefault("schema_version", _SCHEMA_VERSION)
        self._write_snapshot(_IMPORT_FILENAME, payload)

    def write_folders_manifest(self, folders: list[Mapping[str, JSONValue]]) -> None:
        self._write_snapshot(
            _FOLDERS_FILENAME,
            {"schema_version": _SCHEMA_VERSION, "folders": cast(JSONValue, folders)},
        )

    def append(self, event: Mapping[str, JSONValue]) -> None:
        payload = _validate_event(event)
        if payload["import_uuid"] != self._import_uuid:
            raise ValueError("PST manifest event import_uuid does not match the writer")
        if not _validate_transition(payload, self._events):
            return
        encoded = _encode_event(payload)
        with storage_io():
            self._directory.mkdir(parents=True, exist_ok=True)
            if self._handle is None:
                self._handle = (self._directory / _ITEMS_FILENAME).open("ab")
            self._handle.write(encoded)
        self._events.append(payload)

    def read_events(self) -> list[Mapping[str, JSONValue]]:
        return list(self._events)

    def flush_and_sync(self) -> None:
        if self._handle is not None:
            with storage_io():
                self._handle.flush()
                os.fsync(self._handle.fileno())

    def close(self) -> None:
        self.flush_and_sync()
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> PstManifestWriter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()


class PstManifestReader(BasePstManifestReader):
    """Read and verify one PST import generation's durable records."""

    def __init__(self, root: Path, import_uuid: str) -> None:
        _validate_import_uuid(import_uuid)
        self._directory = root / "manifests" / "pst" / import_uuid

    def read_import_manifest(self) -> Mapping[str, JSONValue]:
        return _read_snapshot(self._directory / _IMPORT_FILENAME, True)

    def read_folders_manifest(self) -> list[Mapping[str, JSONValue]]:
        snapshot = _read_snapshot(self._directory / _FOLDERS_FILENAME, False)
        folders = snapshot["folders"]
        if not isinstance(folders, list):
            raise ManifestCorruptError("PST folder manifest folders must be an array")
        return [cast(Mapping[str, JSONValue], folder) for folder in folders]

    def read_all_events(self) -> Iterator[Mapping[str, JSONValue]]:
        return read_events(self._directory / _ITEMS_FILENAME)


def repair_tail(path: Path) -> int:
    """Truncate one malformed final PST event and return removed bytes."""

    with storage_io():
        before = path.stat().st_size
    for _ in read_events(path):
        pass
    with storage_io():
        after = path.stat().st_size
    return before - after