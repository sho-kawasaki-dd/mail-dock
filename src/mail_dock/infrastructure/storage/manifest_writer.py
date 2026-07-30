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
    {"fetch", "fetch_skipped", "parse_failed", "delete_detected", "moved", "remote_state_unknown"}
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
        "timestamp",
        "deduplicated",
    }
)
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


def _validate_event(event: Mapping[str, JSONValue]) -> dict[str, JSONValue]:
    payload = dict(event)
    if not _is_json_value(payload):
        raise TypeError("manifest events must contain JSON-compatible values")
    event_name = payload.get("event")
    if not isinstance(event_name, str) or event_name not in _MANIFEST_EVENTS:
        raise ValueError(f"unsupported manifest event: {event_name!r}")
    _parse_timestamp(payload.get("timestamp"))
    if event_name == "fetch":
        missing_fields = _FETCH_FIELDS.difference(payload)
        if missing_fields:
            raise ValueError(f"fetch event is missing required fields: {sorted(missing_fields)}")
        if not isinstance(payload["uid"], int) or isinstance(payload["uid"], bool):
            raise TypeError("fetch event uid must be an integer")
        if not isinstance(payload["uidvalidity"], int) or isinstance(payload["uidvalidity"], bool):
            raise TypeError("fetch event uidvalidity must be an integer")
        if not isinstance(payload["size_bytes"], int) or isinstance(payload["size_bytes"], bool):
            raise TypeError("fetch event size_bytes must be an integer")
        if not isinstance(payload["deduplicated"], bool):
            raise TypeError("fetch event deduplicated must be a boolean")
    elif event_name == "fetch_skipped":
        required_fields = {"uid", "uidvalidity", "size_bytes", "reason"}
        skipped_missing_fields = required_fields.difference(payload)
        if skipped_missing_fields:
            raise ValueError(
                "fetch_skipped event is missing required fields: "
                f"{sorted(skipped_missing_fields)}"
            )
        if "relative_path" in payload or "file_hash" in payload:
            raise ValueError("fetch_skipped events must not contain EML path or hash")
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

    ``purge_intent`` and ``purged`` are reserved for Phase 4, where local
    purge safety checks are implemented.
    """

    def __init__(self, root: Path, account_id: str) -> None:
        _validate_account_id(account_id)
        self._root = root
        self._account_id = account_id
        self._handles: dict[Path, BinaryIO] = {}

    def _path_for(self, timestamp: datetime) -> Path:
        return (
            self._root / "manifests" / "imap" / self._account_id / f"events-{timestamp:%Y%m}.jsonl"
        )

    def append(self, event: Mapping[str, JSONValue]) -> None:
        encoded = _encode_event(event)
        path = self._path_for(_parse_timestamp(event.get("timestamp")))
        handle = self._handles.get(path)
        with storage_io():
            if handle is None:
                path.parent.mkdir(parents=True, exist_ok=True)
                handle = path.open("ab")
                self._handles[path] = handle
            handle.write(encoded)

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
                yield event
                offset += len(line)
                line = next_line
        if truncate_at is not None:
            _truncate(path, truncate_at)

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
