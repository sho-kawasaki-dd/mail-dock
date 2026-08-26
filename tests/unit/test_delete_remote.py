from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from mail_dock.domain.errors import (
    PermanentError,
    StorageDetachedError,
    StorageError,
    TransientError,
)
from mail_dock.domain.fetcher import RemoteFolder
from mail_dock.domain.messages import StoredEml
from mail_dock.domain.ports import BaseEmlStorage, BaseManifestReader, BaseManifestWriter, JSONValue
from mail_dock.domain.storage_state import StorageState, StorageStateMachine
from mail_dock.usecases.delete_remote import dry_run, execute, reconcile_uncertain_deletes
from tests.support.fake_fetcher import FakeFetcher
from tests.support.in_memory_repository import InMemoryMessageRepository


class MemoryStorage(BaseEmlStorage):
    def __init__(self, files: Mapping[str, bytes]) -> None:
        self.files = dict(files)

    def save(self, account_id: str, internal_date: Any, raw: bytes) -> StoredEml:
        del account_id, internal_date
        path = "eml/account/message.eml"
        self.files[path] = raw
        return StoredEml(path, hashlib.sha256(raw).hexdigest(), len(raw))

    def reuse(self, relative_path: str, expected_hash: str) -> StoredEml | None:
        raw = self.files.get(relative_path)
        if raw is None or hashlib.sha256(raw).hexdigest() != expected_hash:
            return None
        return StoredEml(relative_path, expected_hash, len(raw), True)

    def read(self, relative_path: str) -> bytes:
        return self.files[relative_path]

    def read_verified(self, relative_path: str, expected_hash: str) -> bytes:
        raw = self.files.get(relative_path)
        if raw is None:
            raise FileNotFoundError(relative_path)
        if hashlib.sha256(raw).hexdigest() != expected_hash:
            raise StorageError("EML file hash does not match expected hash")
        return raw


class MemoryManifest(BaseManifestWriter, BaseManifestReader):
    def __init__(self) -> None:
        self.events: list[dict[str, JSONValue]] = []
        self.flush_count = 0

    def append(self, event: Mapping[str, JSONValue]) -> None:
        self.events.append(dict(event))

    def flush_and_sync(self) -> None:
        self.flush_count += 1

    def checkpoint(self, sequence: int, batch_id: str) -> None:
        self.append(
            {
                "event": "checkpoint",
                "account_id": "account",
                "timestamp": "2026-08-27T00:00:00+00:00",
                "sequence": sequence,
                "batch_id": batch_id,
            }
        )
        self.flush_and_sync()

    def read_all_events(self) -> Iterator[Mapping[str, JSONValue]]:
        yield from self.events

    def read_last_checkpoint(self) -> Mapping[str, JSONValue] | None:
        checkpoints = [event for event in self.events if event.get("event") == "checkpoint"]
        return checkpoints[-1] if checkpoints else None

    def read_events_since_checkpoint(self) -> Iterator[Mapping[str, JSONValue]]:
        last_checkpoint = max(
            (
                index
                for index, event in enumerate(self.events)
                if event.get("event") == "checkpoint"
            ),
            default=-1,
        )
        yield from self.events[last_checkpoint + 1 :]

    def read_incomplete_intents(self) -> Iterator[Mapping[str, JSONValue]]:
        completed = {
            (
                event.get("account_id"),
                event.get("folder_raw_name"),
                event.get("uid"),
                event.get("uidvalidity"),
                event.get("mode"),
            )
            for event in self.events
            if event.get("event") == "remote_delete_completed"
        }
        for event in self.events:
            key = (
                event.get("account_id"),
                event.get("folder_raw_name"),
                event.get("uid"),
                event.get("uidvalidity"),
                event.get("mode"),
            )
            if event.get("event") == "remote_delete_intent" and key not in completed:
                yield event


class DeleteFetcher(FakeFetcher):
    def __init__(self, *, transient: bool = False, uidplus: bool = False) -> None:
        super().__init__(
            folders=(
                RemoteFolder("INBOX", "INBOX", 42),
                RemoteFolder("Trash", "Trash", 7, frozenset({r"\Trash"})),
            )
        )
        self.transient = transient
        self.uidplus = uidplus
        self.calls: list[tuple[str, int, str]] = []

    def supports_uid_expunge(self) -> bool:
        return self.uidplus

    def delete_remote_message(self, raw_name: str, uid: int, *, mode: str = "trash") -> None:
        self.calls.append((raw_name, uid, mode))
        if self.transient:
            raise TransientError("connection dropped after command was sent")
        super().delete_remote_message(raw_name, uid, mode=mode)


def _record(repository: InMemoryMessageRepository, *, message_id: int, raw: bytes) -> str:
    repository.upsert_account({"id": "account"})
    folder_id = repository.upsert_folder(
        {"account_id": "account", "raw_name": "INBOX", "uidvalidity": 42}
    )
    path = f"eml/account/{message_id}.eml"
    repository.add_message(
        {
            "id": message_id,
            "account_id": "account",
            "folder_id": folder_id,
            "uid": message_id,
            "uidvalidity": 42,
            "source_item_key": f"42:{message_id}",
            "remote_state": "present",
            "local_state": "active",
            "relative_path": path,
            "file_hash": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
            "subject": f"Subject {message_id}",
            "date_sent": "2026-08-27T00:00:00+00:00",
            "internal_date": "2026-08-27T00:00:00+00:00",
        },
        {"subject": f"Subject {message_id}", "body_text": "body"},
    )
    return path


def test_dry_run_excludes_invalid_eml_and_missing_contents() -> None:
    repository = InMemoryMessageRepository()
    valid_raw = b"valid"
    valid_path = _record(repository, message_id=1, raw=valid_raw)
    _record(repository, message_id=2, raw=b"other")
    repository.messages[2]["file_hash"] = "0" * 64
    _record(repository, message_id=3, raw=b"no contents")
    del repository.contents[3]
    storage = MemoryStorage(
        {
            valid_path: valid_raw,
            "eml/account/2.eml": b"other",
            "eml/account/3.eml": b"no contents",
        }
    )
    state = StorageStateMachine(StorageState.ATTACHED)

    result = dry_run(repository, storage, message_ids=(1, 2, 3), storage_state=state)

    assert [candidate.message_id for candidate in result.candidates] == [1]
    assert result.total_size_bytes == len(valid_raw)
    assert {item.message_id: item.reason for item in result.exclusions} == {
        2: "hash_mismatch",
        3: "message_contents_missing",
    }


def test_execute_records_intent_then_completion_and_updates_state() -> None:
    repository = InMemoryMessageRepository()
    raw = b"message"
    path = _record(repository, message_id=1, raw=raw)
    storage = MemoryStorage({path: raw})
    state = StorageStateMachine(StorageState.ATTACHED)
    plan = dry_run(repository, storage, message_ids=(1,), storage_state=state)
    manifest = MemoryManifest()
    fetcher = DeleteFetcher()
    fetcher.add_message("INBOX", 1, raw)

    result = execute(
        fetcher,
        repository,
        storage,
        manifest,
        plan=plan,
        mode="trash",
        storage_state=state,
    )

    assert result.completed_ids == (1,)
    assert [event["event"] for event in manifest.events] == [
        "remote_delete_intent",
        "remote_delete_completed",
    ]
    assert repository.messages[1]["remote_state"] == "deleted"
    assert repository.audit_log[0]["operation"] == "remote_delete"
    assert fetcher.calls == [("INBOX", 1, "trash")]
    assert manifest.flush_count == 2


def test_execute_rejects_detached_and_unsafe_expunge_before_imap() -> None:
    repository = InMemoryMessageRepository()
    raw = b"message"
    path = _record(repository, message_id=1, raw=raw)
    storage = MemoryStorage({path: raw})
    attached = StorageStateMachine(StorageState.ATTACHED)
    plan = dry_run(repository, storage, message_ids=(1,), storage_state=attached)
    fetcher = DeleteFetcher()
    manifest = MemoryManifest()

    with pytest.raises(StorageDetachedError):
        execute(
            fetcher,
            repository,
            storage,
            manifest,
            plan=plan,
            storage_state=StorageStateMachine(StorageState.DETACHED),
        )
    with pytest.raises(PermanentError):
        execute(
            fetcher,
            repository,
            storage,
            manifest,
            plan=plan,
            mode="expunge",
            storage_state=attached,
        )
    assert fetcher.calls == []
    assert manifest.events == []


def test_transient_delete_is_recorded_as_uncertain_without_marking_deleted() -> None:
    repository = InMemoryMessageRepository()
    raw = b"message"
    path = _record(repository, message_id=1, raw=raw)
    storage = MemoryStorage({path: raw})
    state = StorageStateMachine(StorageState.ATTACHED)
    plan = dry_run(repository, storage, message_ids=(1,), storage_state=state)
    manifest = MemoryManifest()

    result = execute(
        DeleteFetcher(transient=True),
        repository,
        storage,
        manifest,
        plan=plan,
        storage_state=state,
    )

    assert result.uncertain_ids == (1,)
    assert repository.messages[1]["remote_state"] == "present"
    assert [event["event"] for event in manifest.events] == [
        "remote_delete_intent",
        "remote_delete_uncertain",
    ]


def test_reconcile_marks_uncertain_delete_complete_when_uid_is_gone() -> None:
    repository = InMemoryMessageRepository()
    raw = b"message"
    path = _record(repository, message_id=1, raw=raw)
    storage = MemoryStorage({path: raw})
    state = StorageStateMachine(StorageState.ATTACHED)
    plan = dry_run(repository, storage, message_ids=(1,), storage_state=state)
    manifest = MemoryManifest()
    manifest.append(
        {
            "event": "remote_delete_intent",
            "account_id": plan.candidates[0].account_id,
            "folder_raw_name": "INBOX",
            "uid": 1,
            "uidvalidity": 42,
            "mode": "trash",
            "timestamp": "2026-08-27T00:00:00+00:00",
        }
    )
    fetcher = DeleteFetcher()
    fetcher.add_message("INBOX", 1, raw)
    fetcher.delete_remote_message("INBOX", 1)

    reconcile_uncertain_deletes(fetcher, repository, manifest, storage_state=state)

    assert repository.messages[1]["remote_state"] == "deleted"
    assert manifest.events[-1]["event"] == "remote_delete_completed"
