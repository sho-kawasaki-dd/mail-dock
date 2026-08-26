"""Append-only IMAP manifest persistence and integrity checking.

The EML files and this manifest are the sources of truth; ``metadata.db`` is
only a derived cache. Manifest records are append-only JSONL entries with a
CRC32 suffix so torn writes can be detected and removed safely at the tail.
The read API is also the input for the Phase 4 complete database rebuild.
"""

from __future__ import annotations

import json
import os
import re
import zlib
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, cast

from mail_dock.domain.errors import ManifestCorruptError
from mail_dock.domain.ports import BaseManifestWriter, JSONValue
from mail_dock.infrastructure.storage.detach import storage_io

_MANIFEST_EVENTS = frozenset(
    {
        "fetch",
        "fetch_skipped",
        "parse_failed",
        "delete_detected",
        "moved",
        "remote_state_unknown",
        "checkpoint",
        "account_snapshot",
        "folder_snapshot",
        "purge_intent",
        "purged",
        "remote_delete_intent",
        "remote_delete_completed",
        "remote_delete_uncertain",
    }
)
_FETCH_FIELDS = frozenset(
    {
        "event",
        "account_id",
        "folder_raw_name",
        "uid",
        "uidvalidity",
        "source_item_key",
        "message_id",
        "relative_path",
        "file_hash",
        "size_bytes",
        "internal_date",
        "timestamp",
        "deduplicated",
    }
)
_CHECKPOINT_FIELDS = frozenset({"event", "account_id", "timestamp", "sequence", "batch_id"})
_ACCOUNT_SNAPSHOT_FIELDS = frozenset(
    {
        "event",
        "account_id",
        "provider_type",
        "display_name",
        "host",
        "port",
        "username",
        "timestamp",
    }
)
_FOLDER_SNAPSHOT_FIELDS = frozenset(
    {
        "event",
        "account_id",
        "folder_raw_name",
        "display_name",
        "uidvalidity",
        "delimiter",
        "timestamp",
    }
)
_PURGE_FIELDS = frozenset(
    {
        "event",
        "account_id",
        "source_item_key",
        "relative_path",
        "file_hash",
        "timestamp",
        "shared_reference_count",
        "physical_delete",
    }
)
_REMOTE_DELETE_FIELDS = frozenset(
    {"event", "account_id", "folder_raw_name", "uid", "uidvalidity", "mode", "timestamp"}
)
_MOVED_FIELDS = frozenset(
    {
        "event",
        "account_id",
        "folder_raw_name",
        "moved_to_folder_raw_name",
        "uid",
        "uidvalidity",
        "source_item_key",
        "timestamp",
    }
)
_SECRET_FIELD_PARTS = ("password", "passwd", "secret", "token", "credential")
_CRC_SUFFIX = re.compile(rb"\|CRC32:([0-9a-fA-F]{8})$")


def _is_json_value(value: object) -> bool:
    if value is None or isinstance(value, (bool, int, float, str)):
        return True
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_value(item) for key, item in value.items())
    return False


def _validate_account_id(account_id: str) -> None:
    if (
        not account_id
        or account_id in {".", ".."}
        or "/" in account_id
        or "\\" in account_id
        or "\x00" in account_id
        or any(character in account_id for character in ':*?"<>|')
    ):
        raise ValueError("account_id must be a filesystem-safe single path component")


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("manifest event timestamp must be an ISO-8601 string")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        timestamp = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError("manifest event timestamp is invalid") from error
    if timestamp.tzinfo is None:
        raise ValueError("manifest event timestamp must include a timezone")
    return timestamp.astimezone(UTC)


def _require_fields(
    payload: Mapping[str, JSONValue], fields: frozenset[str], event_name: str
) -> None:
    missing_fields = fields.difference(payload)
    if missing_fields:
        raise ValueError(f"{event_name} event is missing required fields: {sorted(missing_fields)}")


def _require_text(payload: Mapping[str, JSONValue], field: str, event_name: str) -> None:
    if not isinstance(payload[field], str) or not payload[field]:
        raise TypeError(f"{event_name} event {field} must be a non-empty string")


def _validate_checkpoint_sequence(sequence: object, previous: int | None) -> int:
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        raise ValueError("checkpoint event sequence must be a non-negative integer")
    if previous is not None and sequence <= previous:
        raise ValueError("checkpoint event sequence must increase monotonically")
    return sequence


def _validate_event(event: Mapping[str, JSONValue]) -> dict[str, JSONValue]:
    payload = dict(event)
    if not _is_json_value(payload):
        raise TypeError("manifest events must contain JSON-compatible values")
    event_name = payload.get("event")
    if not isinstance(event_name, str) or event_name not in _MANIFEST_EVENTS:
        raise ValueError(f"unsupported manifest event: {event_name!r}")
    _parse_timestamp(payload.get("timestamp"))
    if event_name == "fetch":
        _require_fields(payload, _FETCH_FIELDS, event_name)
        if not isinstance(payload["uid"], int) or isinstance(payload["uid"], bool):
            raise TypeError("fetch event uid must be an integer")
        if not isinstance(payload["uidvalidity"], int) or isinstance(payload["uidvalidity"], bool):
            raise TypeError("fetch event uidvalidity must be an integer")
        if not isinstance(payload["size_bytes"], int) or isinstance(payload["size_bytes"], bool):
            raise TypeError("fetch event size_bytes must be an integer")
        if not isinstance(payload["deduplicated"], bool):
            raise TypeError("fetch event deduplicated must be a boolean")
        if payload["internal_date"] is not None and not isinstance(payload["internal_date"], str):
            raise TypeError("fetch event internal_date must be an ISO-8601 string or null")
        if payload["internal_date"] is not None:
            _parse_timestamp(payload["internal_date"])
    elif event_name == "fetch_skipped":
        required_fields = {"uid", "uidvalidity", "size_bytes", "reason"}
        skipped_missing_fields = required_fields.difference(payload)
        if skipped_missing_fields:
            raise ValueError(
                f"fetch_skipped event is missing required fields: {sorted(skipped_missing_fields)}"
            )
        if "relative_path" in payload or "file_hash" in payload:
            raise ValueError("fetch_skipped events must not contain EML path or hash")
    elif event_name == "checkpoint":
        _require_fields(payload, _CHECKPOINT_FIELDS, event_name)
        _require_text(payload, "account_id", event_name)
        _require_text(payload, "batch_id", event_name)
        _validate_checkpoint_sequence(payload["sequence"], None)
    elif event_name == "account_snapshot":
        _require_fields(payload, _ACCOUNT_SNAPSHOT_FIELDS, event_name)
        _require_text(payload, "account_id", event_name)
        for field in ("provider_type", "display_name", "host", "username"):
            _require_text(payload, field, event_name)
        if not isinstance(payload["port"], int) or isinstance(payload["port"], bool):
            raise TypeError("account_snapshot event port must be an integer")
        secret_fields = [
            field
            for field in payload
            if any(part in field.casefold() for part in _SECRET_FIELD_PARTS)
        ]
        if secret_fields:
            raise ValueError("account_snapshot events must not contain credentials or tokens")
    elif event_name == "folder_snapshot":
        _require_fields(payload, _FOLDER_SNAPSHOT_FIELDS, event_name)
        for field in ("account_id", "folder_raw_name", "display_name"):
            _require_text(payload, field, event_name)
        if payload["uidvalidity"] is not None and (
            not isinstance(payload["uidvalidity"], int) or isinstance(payload["uidvalidity"], bool)
        ):
            raise TypeError("folder_snapshot event uidvalidity must be an integer or null")
        if payload["delimiter"] is not None and not isinstance(payload["delimiter"], str):
            raise TypeError("folder_snapshot event delimiter must be a string or null")
    elif event_name in {"purge_intent", "purged"}:
        _require_fields(payload, _PURGE_FIELDS, event_name)
        for field in ("account_id", "source_item_key", "relative_path", "file_hash"):
            _require_text(payload, field, event_name)
        if (
            not isinstance(payload["shared_reference_count"], int)
            or isinstance(payload["shared_reference_count"], bool)
            or payload["shared_reference_count"] < 0
        ):
            raise TypeError("purge event shared_reference_count must be a non-negative integer")
        if not isinstance(payload["physical_delete"], bool):
            raise TypeError("purge event physical_delete must be a boolean")
    elif event_name in {
        "remote_delete_intent",
        "remote_delete_completed",
        "remote_delete_uncertain",
    }:
        _require_fields(payload, _REMOTE_DELETE_FIELDS, event_name)
        _require_text(payload, "account_id", event_name)
        _require_text(payload, "folder_raw_name", event_name)
        if not isinstance(payload["uid"], int) or isinstance(payload["uid"], bool):
            raise TypeError(f"{event_name} event uid must be an integer")
        if not isinstance(payload["uidvalidity"], int) or isinstance(payload["uidvalidity"], bool):
            raise TypeError(f"{event_name} event uidvalidity must be an integer")
        if payload["mode"] not in {"trash", "expunge"}:
            raise ValueError(f"{event_name} event mode must be 'trash' or 'expunge'")
    elif event_name == "moved":
        _require_fields(payload, _MOVED_FIELDS, event_name)
        for field in (
            "account_id",
            "folder_raw_name",
            "moved_to_folder_raw_name",
            "source_item_key",
        ):
            _require_text(payload, field, event_name)
        if not isinstance(payload["uid"], int) or isinstance(payload["uid"], bool):
            raise TypeError("moved event uid must be an integer")
        if not isinstance(payload["uidvalidity"], int) or isinstance(payload["uidvalidity"], bool):
            raise TypeError("moved event uidvalidity must be an integer")
    return payload


def _encode_event(event: Mapping[str, JSONValue]) -> bytes:
    payload = _validate_event(event)
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False
    ).encode("utf-8")
    checksum = zlib.crc32(encoded) & 0xFFFFFFFF
    return encoded + f"|CRC32:{checksum:08x}".encode("ascii") + b"\n"


def _parse_line(line: bytes) -> Mapping[str, JSONValue]:
    if not line.endswith(b"\n"):
        raise ManifestCorruptError("manifest record is missing its trailing newline")
    content = line[:-1]
    match = _CRC_SUFFIX.search(content)
    if match is None:
        raise ManifestCorruptError("manifest record has no valid CRC32 suffix")
    payload = content[: match.start()]
    if zlib.crc32(payload) & 0xFFFFFFFF != int(match.group(1), 16):
        raise ManifestCorruptError("manifest record CRC32 does not match its payload")
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManifestCorruptError("manifest record contains invalid JSON") from error
    if not isinstance(decoded, dict) or not _is_json_value(decoded):
        raise ManifestCorruptError("manifest record must be a JSON object")
    try:
        return _validate_event(cast(Mapping[str, JSONValue], decoded))
    except (TypeError, ValueError) as error:
        raise ManifestCorruptError("manifest record does not satisfy the event schema") from error


def _truncate(path: Path, size: int) -> None:
    with storage_io(), path.open("r+b") as manifest_file:
        manifest_file.truncate(size)
        manifest_file.flush()
        os.fsync(manifest_file.fileno())


class ManifestWriter(BaseManifestWriter):
    """Write durable, monthly-rotated IMAP manifest events.

    Purge and remote-delete intent/completion events are durable recovery
    records for destructive operations.
    """

    def __init__(self, root: Path, account_id: str) -> None:
        _validate_account_id(account_id)
        self._root = root
        self._account_id = account_id
        self._handles: dict[Path, BinaryIO] = {}
        self._last_checkpoint_sequence = _last_checkpoint_sequence(root, account_id)

    @property
    def last_checkpoint_sequence(self) -> int | None:
        return self._last_checkpoint_sequence

    def _path_for(self, timestamp: datetime) -> Path:
        return (
            self._root / "manifests" / "imap" / self._account_id / f"events-{timestamp:%Y%m}.jsonl"
        )

    def append(self, event: Mapping[str, JSONValue]) -> None:
        payload = _validate_event(event)
        next_sequence = self._last_checkpoint_sequence
        if payload["event"] == "checkpoint":
            next_sequence = _validate_checkpoint_sequence(payload["sequence"], next_sequence)
        encoded = _encode_event(payload)
        path = self._path_for(_parse_timestamp(payload.get("timestamp")))
        handle = self._handles.get(path)
        with storage_io():
            if handle is None:
                path.parent.mkdir(parents=True, exist_ok=True)
                handle = path.open("ab")
                self._handles[path] = handle
            handle.write(encoded)
        self._last_checkpoint_sequence = next_sequence

    def checkpoint(self, sequence: int, batch_id: str) -> None:
        """Append and durably flush a completed synchronization batch marker."""
        self.append(
            {
                "event": "checkpoint",
                "account_id": self._account_id,
                "timestamp": datetime.now(UTC).isoformat(),
                "sequence": sequence,
                "batch_id": batch_id,
            }
        )
        self.flush_and_sync()

    def flush_and_sync(self) -> None:
        with storage_io():
            for handle in self._handles.values():
                handle.flush()
                os.fsync(handle.fileno())

    def close(self) -> None:
        self.flush_and_sync()
        for handle in self._handles.values():
            handle.close()
        self._handles.clear()

    def __enter__(self) -> ManifestWriter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()


def read_events(path: Path) -> Iterator[Mapping[str, JSONValue]]:
    """Yield valid events and detach a malformed final record from ``path``."""

    def iterator() -> Iterator[Mapping[str, JSONValue]]:
        offset = 0
        truncate_at: int | None = None
        last_checkpoint_sequence: int | None = None
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
                if event.get("event") == "checkpoint":
                    try:
                        last_checkpoint_sequence = _validate_checkpoint_sequence(
                            event.get("sequence"), last_checkpoint_sequence
                        )
                    except ValueError as error:
                        raise ManifestCorruptError(
                            "manifest checkpoint sequences are not strictly increasing"
                        ) from error
                yield event
                offset += len(line)
                line = next_line
        if truncate_at is not None:
            _truncate(path, truncate_at)

    return iterator()


def _manifest_paths(root: Path, account_id: str) -> tuple[Path, ...]:
    _validate_account_id(account_id)
    manifest_dir = root / "manifests" / "imap" / account_id
    return tuple(sorted(manifest_dir.glob("events-*.jsonl")))


def read_all_events(root: Path, account_id: str) -> Iterator[Mapping[str, JSONValue]]:
    """Yield all account events in monthly order with global checkpoint checks."""

    def iterator() -> Iterator[Mapping[str, JSONValue]]:
        last_checkpoint_sequence: int | None = None
        for path in _manifest_paths(root, account_id):
            for event in read_events(path):
                if event.get("event") == "checkpoint":
                    try:
                        last_checkpoint_sequence = _validate_checkpoint_sequence(
                            event.get("sequence"), last_checkpoint_sequence
                        )
                    except ValueError as error:
                        raise ManifestCorruptError(
                            "manifest checkpoint sequences are not strictly increasing"
                        ) from error
                yield event

    return iterator()


def _last_checkpoint_sequence(root: Path, account_id: str) -> int | None:
    last_sequence: int | None = None
    for event in read_all_events(root, account_id):
        if event.get("event") == "checkpoint":
            last_sequence = cast(int, event["sequence"])
    return last_sequence


def read_last_checkpoint(root: Path, account_id: str) -> Mapping[str, JSONValue] | None:
    """Return the latest checkpoint across all monthly manifest files."""
    latest: Mapping[str, JSONValue] | None = None
    for event in read_all_events(root, account_id):
        if event.get("event") == "checkpoint":
            latest = event
    return latest


def read_events_since_checkpoint(root: Path, account_id: str) -> Iterator[Mapping[str, JSONValue]]:
    """Yield events after the latest durable checkpoint for an account."""

    def iterator() -> Iterator[Mapping[str, JSONValue]]:
        events = list(read_all_events(root, account_id))
        checkpoint_index = max(
            (index for index, event in enumerate(events) if event.get("event") == "checkpoint"),
            default=-1,
        )
        yield from events[checkpoint_index + 1 :]

    return iterator()


def _operation_key(event: Mapping[str, JSONValue]) -> tuple[object, ...]:
    event_name = str(event["event"])
    if event_name in {"purge_intent", "purged"}:
        return (
            event["account_id"],
            event["source_item_key"],
            event["relative_path"],
            event["file_hash"],
        )
    return (
        event["account_id"],
        event["folder_raw_name"],
        event["uid"],
        event["uidvalidity"],
        event["mode"],
    )


def read_incomplete_intents(root: Path, account_id: str) -> Iterator[Mapping[str, JSONValue]]:
    """Yield destructive-operation intents without a matching completion event."""

    def iterator() -> Iterator[Mapping[str, JSONValue]]:
        events = list(read_all_events(root, account_id))
        completed = {
            _operation_key(event)
            for event in events
            if event["event"] in {"purged", "remote_delete_completed"}
        }
        for event in events:
            if (
                event["event"] in {"purge_intent", "remote_delete_intent"}
                and _operation_key(event) not in completed
            ):
                yield event

    return iterator()


def repair_tail(path: Path) -> int:
    """Truncate one malformed final record and return the removed byte count."""
    with storage_io():
        before = path.stat().st_size
    for _ in read_events(path):
        pass
    with storage_io():
        after = path.stat().st_size
    return before - after
