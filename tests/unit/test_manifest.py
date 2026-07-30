import json
import zlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mail_dock.domain.errors import ManifestCorruptError
from mail_dock.domain.ports import JSONValue
from mail_dock.infrastructure.storage.manifest import ManifestWriter, read_events, repair_tail


def _fetch_event(timestamp: str, *, uid: int = 7) -> dict[str, JSONValue]:
    return {
        "event": "fetch",
        "account_id": "user@example.com",
        "folder_raw_name": "INBOX",
        "uid": uid,
        "uidvalidity": 42,
        "source_item_key": f"42:{uid}",
        "message_id": "<message@example.com>",
        "relative_path": "eml/user@example.com/2026/07/abc.eml",
        "file_hash": "a" * 64,
        "size_bytes": 123,
        "timestamp": timestamp,
        "deduplicated": False,
    }


def test_manifest_appends_crc_and_rotates_months(tmp_path: Path) -> None:
    with ManifestWriter(tmp_path, "user@example.com") as writer:
        writer.append(_fetch_event("2026-07-30T12:34:56Z"))
        writer.append(_fetch_event("2026-08-01T00:00:00Z", uid=8))
        writer.flush_and_sync()

    july = tmp_path / "manifests/imap/user@example.com/events-202607.jsonl"
    august = tmp_path / "manifests/imap/user@example.com/events-202608.jsonl"
    line = july.read_bytes().splitlines(keepends=True)[0]
    payload, suffix = line[:-1].rsplit(b"|CRC32:", 1)

    assert suffix == f"{zlib.crc32(payload) & 0xFFFFFFFF:08x}".encode()
    assert next(iter(read_events(july)))["uid"] == 7
    assert next(iter(read_events(august)))["uid"] == 8


def test_read_events_repairs_only_a_torn_tail(tmp_path: Path) -> None:
    path = tmp_path / "events-202607.jsonl"
    with ManifestWriter(tmp_path, "account") as writer:
        writer.append(_fetch_event("2026-07-30T12:34:56Z"))
        writer.flush_and_sync()
    source = tmp_path / "manifests/imap/account/events-202607.jsonl"
    path.write_bytes(source.read_bytes() + b'{"event":"fetch"')

    events = list(read_events(path))

    assert len(events) == 1
    assert path.read_bytes() == source.read_bytes()
    assert repair_tail(path) == 0


def test_repair_tail_reports_removed_bytes(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    payload = json.dumps({"event": "fetch"}).encode()
    path.write_bytes(payload + b"|CRC32:00000000\n")

    removed = repair_tail(path)

    assert removed == len(payload) + len(b"|CRC32:00000000\n")
    assert path.read_bytes() == b""


def test_middle_corruption_is_not_repaired(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    with ManifestWriter(tmp_path, "account") as writer:
        writer.append(_fetch_event("2026-07-30T12:34:56Z"))
        writer.append(_fetch_event("2026-07-30T12:35:56Z", uid=8))
        writer.flush_and_sync()
    source = tmp_path / "manifests/imap/account/events-202607.jsonl"
    lines = source.read_bytes().splitlines(keepends=True)
    lines[0] = lines[0].replace(b"CRC32:", b"CRC33:")
    path.write_bytes(b"".join(lines))

    with pytest.raises(ManifestCorruptError):
        list(read_events(path))


def test_fetch_skipped_cannot_point_to_an_eml(tmp_path: Path) -> None:
    event: dict[str, JSONValue] = {
        "event": "fetch_skipped",
        "uid": 7,
        "uidvalidity": 42,
        "size_bytes": 100,
        "reason": "oversize",
        "timestamp": datetime.now(UTC).isoformat(),
        "relative_path": "eml/file.eml",
    }

    with ManifestWriter(tmp_path, "account") as writer, pytest.raises(ValueError):
        writer.append(event)
