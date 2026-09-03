import json
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from mail_dock.domain.errors import ManifestCorruptError
from mail_dock.domain.ports import JSONValue
from mail_dock.infrastructure.storage.manifest import (
    ManifestWriter,
    read_all_events,
    read_events,
    read_events_since_checkpoint,
    read_incomplete_intents,
    read_last_checkpoint,
    repair_tail,
)


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
        "internal_date": "2026-07-30T12:00:00Z",
        "timestamp": timestamp,
        "deduplicated": False,
    }


@pytest.mark.parametrize(
    "event_name",
    (
        "fetch",
        "fetch_skipped",
        "parse_failed",
        "delete_detected",
        "moved",
        "remote_state_unknown",
    ),
)
def test_manifest_accepts_all_phase_one_event_types(tmp_path: Path, event_name: str) -> None:
    event: dict[str, JSONValue] = {
        "event": event_name,
        "timestamp": "2026-07-30T12:34:56Z",
    }
    if event_name == "fetch":
        event = _fetch_event("2026-07-30T12:34:56Z")
    elif event_name == "fetch_skipped":
        event.update({"uid": 7, "uidvalidity": 42, "size_bytes": 100, "reason": "oversize"})
    elif event_name == "moved":
        event.update(
            {
                "account_id": "user@example.com",
                "folder_raw_name": "INBOX",
                "moved_to_folder_raw_name": "Archive",
                "uid": 7,
                "uidvalidity": 42,
                "source_item_key": "42:7",
            }
        )

    with ManifestWriter(tmp_path, "account") as writer:
        writer.append(event)

    path = tmp_path / "manifests/imap/account/events-202607.jsonl"
    assert list(read_events(path)) == [event]


@pytest.mark.parametrize(
    "field",
    (
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
    ),
)
def test_fetch_requires_all_manifest_fields(tmp_path: Path, field: str) -> None:
    event = _fetch_event("2026-07-30T12:34:56Z")
    del event[field]

    with ManifestWriter(tmp_path, "account") as writer, pytest.raises(ValueError):
        writer.append(event)


def test_fetch_event_without_internal_date_is_readable(tmp_path: Path) -> None:
    event = _fetch_event("2026-07-30T12:34:56Z")
    del event["internal_date"]

    with ManifestWriter(tmp_path, "account") as writer:
        writer.append(event)

    path = tmp_path / "manifests/imap/account/events-202607.jsonl"
    read_back = list(read_events(path))
    assert read_back == [event]


def test_manifest_flush_and_sync_is_the_fsync_boundary(tmp_path: Path) -> None:
    writer = ManifestWriter(tmp_path, "account")
    try:
        with patch("mail_dock.infrastructure.storage.manifest.os.fsync") as fsync:
            writer.append(_fetch_event("2026-07-30T12:34:56Z"))
            fsync.assert_not_called()

            writer.flush_and_sync()

            assert fsync.call_count == 1
    finally:
        writer.close()


def _valid_manifest_with_tail(tmp_path: Path, tail: bytes) -> Path:
    with ManifestWriter(tmp_path, "account") as writer:
        writer.append(_fetch_event("2026-07-30T12:34:56Z"))
    source = tmp_path / "manifests/imap/account/events-202607.jsonl"
    path = tmp_path / "events-with-tail.jsonl"
    path.write_bytes(source.read_bytes() + tail)
    return path


@pytest.mark.parametrize("tail_kind", ("missing_newline", "bad_crc", "invalid_json"))
def test_read_events_repairs_each_kind_of_malformed_tail(tmp_path: Path, tail_kind: str) -> None:
    if tail_kind == "missing_newline":
        tail = b'{"event":"fetch"}'
    elif tail_kind == "bad_crc":
        tail = b'{"event":"fetch"}|CRC32:00000000\n'
    else:
        payload = b"not-json"
        checksum = zlib.crc32(payload) & 0xFFFFFFFF
        tail = payload + f"|CRC32:{checksum:08x}\n".encode("ascii")

    path = _valid_manifest_with_tail(tmp_path, tail)
    source = path.parent / "manifests/imap/account/events-202607.jsonl"

    assert len(list(read_events(path))) == 1
    assert path.read_bytes() == source.read_bytes()


def test_manifest_appends_without_rewriting_existing_records(tmp_path: Path) -> None:
    path = tmp_path / "manifests/imap/account/events-202607.jsonl"
    with ManifestWriter(tmp_path, "account") as writer:
        writer.append(_fetch_event("2026-07-30T12:34:56Z", uid=7))
        writer.flush_and_sync()

    with ManifestWriter(tmp_path, "account") as writer:
        writer.append(_fetch_event("2026-07-30T12:35:56Z", uid=8))
        writer.flush_and_sync()

    assert [event["uid"] for event in read_events(path)] == [7, 8]


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
    lines[0] = lines[0].replace(b'"uid":7', b'"uid":9')
    path.write_bytes(b"".join(lines))

    with pytest.raises(ManifestCorruptError):
        list(read_events(path))
    assert path.read_bytes() == b"".join(lines)


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


def _purge_event(event_name: str, timestamp: str) -> dict[str, JSONValue]:
    return {
        "event": event_name,
        "account_id": "account",
        "source_item_key": "42:7",
        "relative_path": "eml/account/2026/07/message.eml",
        "file_hash": "b" * 64,
        "timestamp": timestamp,
        "shared_reference_count": 0,
        "physical_delete": True,
    }


def _remote_delete_event(event_name: str, timestamp: str) -> dict[str, JSONValue]:
    return {
        "event": event_name,
        "account_id": "account",
        "folder_raw_name": "INBOX",
        "uid": 7,
        "uidvalidity": 42,
        "mode": "trash",
        "timestamp": timestamp,
    }


@pytest.mark.parametrize(
    "event",
    (
        {
            "event": "checkpoint",
            "account_id": "account",
            "timestamp": "2026-07-30T12:34:56Z",
            "sequence": 1,
            "batch_id": "batch-1",
        },
        {
            "event": "account_snapshot",
            "account_id": "account",
            "provider_type": "imap",
            "display_name": "Example",
            "host": "imap.example.test",
            "port": 993,
            "username": "user@example.test",
            "timestamp": "2026-07-30T12:34:56Z",
        },
        {
            "event": "folder_snapshot",
            "account_id": "account",
            "folder_raw_name": "INBOX",
            "display_name": "受信箱",
            "uidvalidity": 42,
            "delimiter": "/",
            "timestamp": "2026-07-30T12:34:56Z",
        },
        _purge_event("purge_intent", "2026-07-30T12:34:56Z"),
        _purge_event("purged", "2026-07-30T12:35:56Z"),
        _remote_delete_event("remote_delete_intent", "2026-07-30T12:34:56Z"),
        _remote_delete_event("remote_delete_completed", "2026-07-30T12:35:56Z"),
        _remote_delete_event("remote_delete_uncertain", "2026-07-30T12:35:56Z"),
    ),
)
def test_manifest_accepts_phase_four_event_schemas(
    tmp_path: Path, event: dict[str, JSONValue]
) -> None:
    with ManifestWriter(tmp_path, "account") as writer:
        writer.append(event)


def test_phase_four_events_require_all_schema_fields(tmp_path: Path) -> None:
    cases: tuple[tuple[dict[str, JSONValue], str], ...] = (
        (
            {
                "event": "checkpoint",
                "account_id": "account",
                "timestamp": "2026-07-30T12:34:56Z",
                "sequence": 1,
                "batch_id": "batch-1",
            },
            "batch_id",
        ),
        (
            {
                "event": "account_snapshot",
                "account_id": "account",
                "provider_type": "imap",
                "display_name": "Example",
                "host": "imap.example.test",
                "port": 993,
                "username": "user@example.test",
                "timestamp": "2026-07-30T12:34:56Z",
            },
            "host",
        ),
        (
            {
                "event": "folder_snapshot",
                "account_id": "account",
                "folder_raw_name": "INBOX",
                "display_name": "受信箱",
                "uidvalidity": 42,
                "delimiter": "/",
                "timestamp": "2026-07-30T12:34:56Z",
            },
            "uidvalidity",
        ),
        (_purge_event("purge_intent", "2026-07-30T12:34:56Z"), "file_hash"),
        (_remote_delete_event("remote_delete_intent", "2026-07-30T12:34:56Z"), "mode"),
    )

    for event, field in cases:
        malformed = dict(event)
        del malformed[field]
        with ManifestWriter(tmp_path, "account") as writer, pytest.raises(ValueError):
            writer.append(malformed)


def test_account_snapshot_rejects_secret_fields(tmp_path: Path) -> None:
    event: dict[str, JSONValue] = {
        "event": "account_snapshot",
        "account_id": "account",
        "provider_type": "imap",
        "display_name": "Example",
        "host": "imap.example.test",
        "port": 993,
        "username": "user@example.test",
        "access_token": "must-not-be-recorded",
        "timestamp": "2026-07-30T12:34:56Z",
    }

    with ManifestWriter(tmp_path, "account") as writer, pytest.raises(ValueError):
        writer.append(event)


def test_checkpoint_sequence_is_monotonic_across_months(tmp_path: Path) -> None:
    with ManifestWriter(tmp_path, "account") as writer:
        writer.checkpoint(1, "batch-1")
        writer.append(_fetch_event("2026-08-01T00:00:00Z", uid=8))
        writer.checkpoint(2, "batch-2")
        with pytest.raises(ValueError):
            writer.checkpoint(2, "duplicate")

    assert read_last_checkpoint(tmp_path, "account") == {
        "event": "checkpoint",
        "account_id": "account",
        "timestamp": next(
            event["timestamp"]
            for event in read_all_events(tmp_path, "account")
            if event["event"] == "checkpoint" and event["sequence"] == 2
        ),
        "sequence": 2,
        "batch_id": "batch-2",
    }
    assert [event["uid"] for event in read_events_since_checkpoint(tmp_path, "account")] == []


def test_manifest_reads_events_after_last_checkpoint(tmp_path: Path) -> None:
    event_timestamp = datetime.now(UTC).isoformat()
    with ManifestWriter(tmp_path, "account") as writer:
        writer.append(_fetch_event(event_timestamp, uid=7))
        writer.checkpoint(1, "batch-1")
        writer.append(_fetch_event(event_timestamp, uid=8))

    events = list(read_events_since_checkpoint(tmp_path, "account"))

    assert [event["uid"] for event in events] == [8]


def test_manifest_reads_only_events_after_the_latest_checkpoint(tmp_path: Path) -> None:
    checkpoint_reference = datetime.now(UTC)
    before_checkpoint = (checkpoint_reference - timedelta(days=1)).isoformat()
    after_checkpoint = (checkpoint_reference + timedelta(days=1)).isoformat()

    with ManifestWriter(tmp_path, "account") as writer:
        writer.append(_fetch_event(before_checkpoint, uid=7))
        writer.checkpoint(1, "batch-1")
        writer.append(_fetch_event(before_checkpoint, uid=8))
        writer.checkpoint(2, "batch-2")
        writer.append(_fetch_event(after_checkpoint, uid=9))

    assert [event["uid"] for event in read_events_since_checkpoint(tmp_path, "account")] == [9]
    checkpoint = read_last_checkpoint(tmp_path, "account")
    assert checkpoint is not None
    assert checkpoint["sequence"] == 2


def test_manifest_lists_only_incomplete_destructive_intents(tmp_path: Path) -> None:
    with ManifestWriter(tmp_path, "account") as writer:
        writer.append(_purge_event("purge_intent", "2026-07-30T12:34:56Z"))
        writer.append(_purge_event("purged", "2026-07-30T12:35:56Z"))
        writer.append(_remote_delete_event("remote_delete_intent", "2026-07-30T12:36:56Z"))

    incomplete = list(read_incomplete_intents(tmp_path, "account"))

    assert [event["event"] for event in incomplete] == ["remote_delete_intent"]
