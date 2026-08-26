from __future__ import annotations

import hashlib
import zlib
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from mail_dock.domain.errors import ManifestCorruptError
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.ports import BaseIntegrityStorage, BaseManifestReader, JSONValue
from mail_dock.usecases.verify import (
    VerifyProgress,
    full_verify,
    orphan_scan,
    quick_verify,
    range_verify,
    verify_manifest,
)
from tests.support.in_memory_repository import InMemoryMessageRepository


class MemoryIntegrityStorage(BaseIntegrityStorage):
    def __init__(self, files: Mapping[str, bytes]) -> None:
        self.files = dict(files)
        self.quarantined: list[str] = []
        self.chunk_sizes: list[int] = []

    def stat(self, relative_path: str) -> Any:
        if relative_path not in self.files:
            raise FileNotFoundError(relative_path)

        class FileStat:
            st_size = len(self.files[relative_path])

        return FileStat()

    def iter_chunks(self, relative_path: str, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        if relative_path not in self.files:
            raise FileNotFoundError(relative_path)
        payload = self.files[relative_path]
        for start in range(0, len(payload), chunk_size):
            self.chunk_sizes.append(min(chunk_size, len(payload) - start))
            yield payload[start : start + chunk_size]

    def iter_eml_paths(self, account_id: str | None = None) -> Iterator[str]:
        del account_id
        yield from sorted(self.files)

    def quarantine(self, relative_path: str) -> None:
        self.quarantined.append(relative_path)
        self.files.pop(relative_path, None)


class MemoryManifestReader(BaseManifestReader):
    def __init__(self, events: list[Mapping[str, JSONValue]]) -> None:
        self.events = events

    def read_all_events(self) -> Iterator[Mapping[str, JSONValue]]:
        yield from self.events

    def read_last_checkpoint(self) -> Mapping[str, JSONValue] | None:
        return next(
            (event for event in reversed(self.events) if event.get("event") == "checkpoint"),
            None,
        )

    def read_events_since_checkpoint(self) -> Iterator[Mapping[str, JSONValue]]:
        checkpoint_index = max(
            (
                index
                for index, event in enumerate(self.events)
                if event.get("event") == "checkpoint"
            ),
            default=-1,
        )
        yield from self.events[checkpoint_index + 1 :]

    def read_incomplete_intents(self) -> Iterator[Mapping[str, JSONValue]]:
        yield from ()


def _add_message(
    repo: InMemoryMessageRepository,
    *,
    uid: int,
    relative_path: str,
    raw: bytes,
    source_item_key: str | None = None,
    size_bytes: int | None = None,
) -> int:
    return repo.add_message(
        {
            "account_id": "account",
            "folder_id": 1,
            "uid": uid,
            "uidvalidity": 10,
            "source_item_key": source_item_key or f"10:{uid}",
            "relative_path": relative_path,
            "file_hash": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw) if size_bytes is None else size_bytes,
        }
    )


def _fetch_event(relative_path: str, raw: bytes, uid: int = 1) -> dict[str, JSONValue]:
    return {
        "event": "fetch",
        "account_id": "account",
        "folder_raw_name": "INBOX",
        "uid": uid,
        "uidvalidity": 10,
        "source_item_key": f"10:{uid}",
        "message_id": None,
        "relative_path": relative_path,
        "file_hash": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "internal_date": None,
        "timestamp": "2026-08-26T00:00:00Z",
        "deduplicated": False,
    }


def test_quick_verify_reports_missing_and_size_mismatch() -> None:
    repo = InMemoryMessageRepository()
    raw = b"one"
    _add_message(repo, uid=1, relative_path="present.eml", raw=raw)
    _add_message(repo, uid=2, relative_path="missing.eml", raw=raw)
    _add_message(repo, uid=3, relative_path="wrong-size.eml", raw=raw, size_bytes=99)
    storage = MemoryIntegrityStorage({"present.eml": raw, "wrong-size.eml": raw})

    result = quick_verify(repo, storage)

    assert result.checked_count == 3
    assert result.missing_paths == ("missing.eml",)
    assert result.size_mismatch_paths == ("wrong-size.eml",)


def test_range_verify_uses_only_events_after_checkpoint_and_repairs_mismatch() -> None:
    repo = InMemoryMessageRepository()
    old_raw = b"old"
    current_raw = b"current"
    message_id = _add_message(repo, uid=2, relative_path="current.eml", raw=current_raw)
    storage = MemoryIntegrityStorage({"current.eml": b"tampered"})
    reader = MemoryManifestReader(
        [
            _fetch_event("old.eml", old_raw, uid=1),
            {"event": "checkpoint", "sequence": 1},
            _fetch_event("current.eml", current_raw, uid=2),
        ]
    )

    result = range_verify(repo, storage, reader)

    assert result.checked_count == 1
    assert result.mismatch_count == 1
    assert result.quarantined_count == 1
    assert storage.quarantined == ["current.eml"]
    assert repo.messages[message_id]["relative_path"] is None
    assert repo.failures[("account", 1, 10, 2)]["error_class"] == "integrity"


def test_full_verify_hashes_using_bounded_chunks_and_reports_progress() -> None:
    raw = b"0123456789"
    storage = MemoryIntegrityStorage({"eml/account/message.eml": raw})
    repo = InMemoryMessageRepository()
    _add_message(repo, uid=1, relative_path="eml/account/message.eml", raw=raw)
    progress: list[VerifyProgress] = []

    result = full_verify(
        repo,
        storage,
        on_progress=progress.append,
    )

    assert result.checked_count == 1
    assert not result.issues
    assert progress[-1].checked_count == 1
    assert storage.chunk_sizes == [10]


def test_orphan_scan_only_marks_manifest_provenance_as_registerable() -> None:
    registered_raw = b"registered orphan"
    unknown_raw = b"unknown orphan"
    registered_path = "eml/account/registered.eml"
    unknown_path = "eml/account/unknown.eml"
    repo = InMemoryMessageRepository()
    storage = MemoryIntegrityStorage({registered_path: registered_raw, unknown_path: unknown_raw})
    reader = MemoryManifestReader([_fetch_event(registered_path, registered_raw)])

    result = orphan_scan(repo, storage, manifest_reader=reader)

    assert result.registerable_paths == (registered_path,)
    assert result.quarantined_paths == (unknown_path,)
    assert storage.quarantined == [unknown_path]
    assert repo.audit_log[0]["operation"] == "orphan_quarantine"


def test_orphan_scan_does_not_register_a_duplicate_hash() -> None:
    raw = b"already indexed"
    known_path = "eml/account/known.eml"
    duplicate_path = "eml/account/duplicate.eml"
    repo = InMemoryMessageRepository()
    _add_message(repo, uid=1, relative_path=known_path, raw=raw)
    storage = MemoryIntegrityStorage({duplicate_path: raw})
    reader = MemoryManifestReader([_fetch_event(duplicate_path, raw, uid=2)])

    result = orphan_scan(repo, storage, manifest_reader=reader)

    assert result.registerable == ()
    assert result.quarantined_paths == (duplicate_path,)


def test_orphan_scan_can_be_cancelled_before_physical_actions() -> None:
    token = CancelToken()
    token.cancel()

    result = orphan_scan(
        InMemoryMessageRepository(),
        MemoryIntegrityStorage({"orphan.eml": b"orphan"}),
        cancel=token,
    )

    assert result.cancelled
    assert result.checked_count == 0


def test_verify_manifest_repairs_only_a_malformed_tail(tmp_path: Path) -> None:
    manifest = tmp_path / "manifests" / "imap" / "account" / "events-202608.jsonl"
    manifest.parent.mkdir(parents=True)
    payload = b'{"event":"checkpoint"}'
    record = payload + f"|CRC32:{zlib.crc32(payload) & 0xFFFFFFFF:08x}\n".encode()
    manifest.write_bytes(record + b'{"event":"fetch"')

    result = verify_manifest(tmp_path)

    assert result.files_checked == 1
    assert result.records_checked == 1
    assert result.repaired_bytes == len(b'{"event":"fetch"')
    assert manifest.read_bytes() == record


def test_verify_manifest_does_not_repair_middle_corruption(tmp_path: Path) -> None:
    manifest = tmp_path / "manifests" / "imap" / "account" / "events-202608.jsonl"
    manifest.parent.mkdir(parents=True)
    payload = b'{"event":"checkpoint"}'
    record = payload + f"|CRC32:{zlib.crc32(payload) & 0xFFFFFFFF:08x}\n".encode()
    manifest.write_bytes(record.replace(b"checkpoint", b"corrupted") + record)

    with pytest.raises(ManifestCorruptError):
        verify_manifest(tmp_path)
