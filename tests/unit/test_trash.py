from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime

import pytest

from mail_dock.domain.errors import StorageDetachedError
from mail_dock.domain.ports import (
    BaseManifestReader,
    BaseManifestWriter,
    BasePurgeStorage,
    JSONValue,
)
from mail_dock.usecases.trash import (
    list_purge_candidates,
    list_startup_purge_candidates,
    move_to_trash,
    purge,
    recover_incomplete_purges,
    restore_from_trash,
)
from tests.support.in_memory_repository import InMemoryMessageRepository


class MemoryPurgeStorage(BasePurgeStorage):
    def __init__(self, files: Mapping[str, bytes], trace: list[str] | None = None) -> None:
        self.files = dict(files)
        self.calls: list[str] = []
        self.trace = trace if trace is not None else []

    def exists(self, relative_path: str) -> bool:
        self.calls.append(f"exists:{relative_path}")
        self.trace.append(f"exists:{relative_path}")
        return relative_path in self.files

    def delete(self, relative_path: str) -> None:
        self.calls.append(f"delete:{relative_path}")
        self.trace.append(f"delete:{relative_path}")
        self.files.pop(relative_path, None)


class MemoryManifest(BaseManifestWriter, BaseManifestReader):
    def __init__(
        self,
        events: list[Mapping[str, JSONValue]] | None = None,
        trace: list[str] | None = None,
    ) -> None:
        self.events = list(events or [])
        self.calls: list[str] = []
        self.trace = trace if trace is not None else []

    def append(self, event: Mapping[str, JSONValue]) -> None:
        self.calls.append(f"append:{event['event']}")
        self.trace.append(f"append:{event['event']}")
        self.events.append(dict(event))

    def flush_and_sync(self) -> None:
        self.calls.append("fsync")
        self.trace.append("fsync")

    def checkpoint(self, sequence: int, batch_id: str) -> None:
        del sequence, batch_id
        raise NotImplementedError

    def read_all_events(self) -> Iterator[Mapping[str, JSONValue]]:
        yield from self.events

    def read_last_checkpoint(self) -> Mapping[str, JSONValue] | None:
        return next(
            (event for event in reversed(self.events) if event.get("event") == "checkpoint"),
            None,
        )

    def read_events_since_checkpoint(self) -> Iterator[Mapping[str, JSONValue]]:
        yield from self.events

    def read_incomplete_intents(self) -> Iterator[Mapping[str, JSONValue]]:
        completed = {
            (event.get("account_id"), event.get("source_item_key"))
            for event in self.events
            if event.get("event") == "purged"
        }
        yield from (
            event
            for event in self.events
            if event.get("event") == "purge_intent"
            and (event.get("account_id"), event.get("source_item_key")) not in completed
        )


class AttachedState:
    def is_write_allowed(self) -> bool:
        return True


class DetachedState:
    def is_write_allowed(self) -> bool:
        return False


def _message(
    repo: InMemoryMessageRepository,
    *,
    uid: int,
    path: str,
    raw: bytes,
    local_state: str = "active",
    trashed_at: str | None = None,
) -> int:
    return repo.add_message(
        {
            "account_id": "account",
            "folder_id": 1,
            "uid": uid,
            "uidvalidity": 10,
            "source_item_key": f"10:{uid}",
            "relative_path": path,
            "file_hash": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
            "local_state": local_state,
            "trashed_at": trashed_at,
        },
        {"body_text": "body"},
    )


def test_move_and_restore_only_change_database_state() -> None:
    repo = InMemoryMessageRepository()
    message_id = _message(repo, uid=1, path="message.eml", raw=b"message")

    moved = move_to_trash(
        repo,
        message_ids=[message_id],
        now=datetime(2026, 8, 27, tzinfo=UTC),
    )

    assert moved.trashed_ids == (message_id,)
    assert repo.messages[message_id]["relative_path"] == "message.eml"
    assert repo.messages[message_id]["local_state"] == "trashed"
    assert repo.messages[message_id]["trashed_at"] == "2026-08-27T00:00:00+00:00"

    restored = restore_from_trash(repo, message_ids=[message_id])

    assert restored.restored_ids == (message_id,)
    assert repo.messages[message_id]["local_state"] == "active"
    assert repo.messages[message_id]["trashed_at"] is None


def test_list_purge_candidates_uses_grace_period() -> None:
    repo = InMemoryMessageRepository()
    old = _message(
        repo,
        uid=1,
        path="old.eml",
        raw=b"old",
        local_state="trashed",
        trashed_at="2026-07-20T00:00:00+00:00",
    )
    recent = _message(
        repo,
        uid=2,
        path="recent.eml",
        raw=b"recent",
        local_state="trashed",
        trashed_at="2026-08-10T00:00:00+00:00",
    )

    candidates = list_purge_candidates(
        repo,
        now=datetime(2026, 8, 27, tzinfo=UTC),
        grace_days=30,
    )

    assert [record["id"] for record in candidates] == [old]
    assert recent not in [record["id"] for record in candidates]


@pytest.mark.parametrize(
    ("mode", "expected_ids"),
    [("manual", ()), ("grace", (1,)), ("immediate", (1,))],
)
def test_startup_purge_candidates_follow_mode(mode: str, expected_ids: tuple[int, ...]) -> None:
    repo = InMemoryMessageRepository()
    _message(
        repo,
        uid=1,
        path="old.eml",
        raw=b"old",
        local_state="trashed",
        trashed_at="2026-07-20T00:00:00+00:00",
    )

    candidates = list_startup_purge_candidates(
        repo,
        mode=mode,
        now=datetime(2026, 8, 27, tzinfo=UTC),
        grace_days=30,
    )

    assert tuple(record["id"] for record in candidates) == expected_ids


def test_purge_preserves_shared_eml_until_last_reference() -> None:
    repo = InMemoryMessageRepository()
    raw = b"shared"
    first = _message(repo, uid=1, path="shared.eml", raw=raw, local_state="trashed")
    second = _message(repo, uid=2, path="shared.eml", raw=raw, local_state="active")
    storage = MemoryPurgeStorage({"shared.eml": raw})
    manifest = MemoryManifest()

    first_result = purge(
        repo,
        storage,
        manifest,
        message_ids=[first],
        storage_state=AttachedState(),
    )

    assert first_result.purged_ids == (first,)
    assert first_result.shared_paths == ("shared.eml",)
    assert "shared.eml" in storage.files
    assert repo.messages[first]["local_state"] == "purged"
    assert repo.messages[first]["relative_path"] is None
    assert repo.messages[first]["file_hash"] is None
    assert first not in repo.contents
    assert repo.messages[second]["local_state"] == "active"

    move_to_trash(repo, message_ids=[second], now=datetime.now(UTC))
    second_result = purge(
        repo,
        storage,
        manifest,
        message_ids=[second],
        storage_state=AttachedState(),
    )

    assert second_result.physically_deleted_paths == ("shared.eml",)
    assert "shared.eml" not in storage.files
    assert [entry["operation"] for entry in repo.audit_log] == [
        "local_purge",
        "local_purge",
    ]


def test_purge_orders_intent_fsync_delete_completion_fsync_before_db() -> None:
    repo = InMemoryMessageRepository()
    message_id = _message(repo, uid=1, path="message.eml", raw=b"message", local_state="trashed")
    trace: list[str] = []
    storage = MemoryPurgeStorage({"message.eml": b"message"}, trace)
    manifest = MemoryManifest(trace=trace)

    purge(repo, storage, manifest, message_ids=[message_id], storage_state=AttachedState())

    assert trace == [
        "append:purge_intent",
        "fsync",
        "exists:message.eml",
        "delete:message.eml",
        "append:purged",
        "fsync",
    ]
    assert repo.messages[message_id]["local_state"] == "purged"


def test_purge_rejects_detached_storage_before_reading_messages() -> None:
    repo = InMemoryMessageRepository()
    storage = MemoryPurgeStorage({})
    manifest = MemoryManifest()

    with pytest.raises(StorageDetachedError):
        purge(repo, storage, manifest, message_ids=[1], storage_state=DetachedState())

    assert not storage.calls
    assert not manifest.calls


def test_recover_incomplete_purge_resumes_after_existing_intent() -> None:
    repo = InMemoryMessageRepository()
    message_id = _message(repo, uid=1, path="message.eml", raw=b"message", local_state="trashed")
    timestamp = "2026-08-27T00:00:00+00:00"
    manifest = MemoryManifest(
        [
            {
                "event": "purge_intent",
                "account_id": "account",
                "source_item_key": "10:1",
                "relative_path": "message.eml",
                "file_hash": hashlib.sha256(b"message").hexdigest(),
                "timestamp": timestamp,
                "shared_reference_count": 0,
                "physical_delete": True,
            }
        ]
    )
    storage = MemoryPurgeStorage({"message.eml": b"message"})

    recover_incomplete_purges(
        repo,
        storage,
        manifest,
        storage_state=AttachedState(),
    )

    assert message_id == 1
    assert [event["event"] for event in manifest.events] == ["purge_intent", "purged"]
    assert repo.messages[message_id]["local_state"] == "purged"
    assert "message.eml" not in storage.files


def test_recover_incomplete_purge_finishes_when_eml_was_already_deleted() -> None:
    """A crash right after the physical delete leaves no file but no ``purged`` event."""
    repo = InMemoryMessageRepository()
    message_id = _message(repo, uid=1, path="message.eml", raw=b"message", local_state="trashed")
    timestamp = "2026-08-27T00:00:00+00:00"
    manifest = MemoryManifest(
        [
            {
                "event": "purge_intent",
                "account_id": "account",
                "source_item_key": "10:1",
                "relative_path": "message.eml",
                "file_hash": hashlib.sha256(b"message").hexdigest(),
                "timestamp": timestamp,
                "shared_reference_count": 0,
                "physical_delete": True,
            }
        ]
    )
    storage = MemoryPurgeStorage({})  # file already gone before the crash

    recover_incomplete_purges(repo, storage, manifest, storage_state=AttachedState())

    assert "delete:message.eml" not in storage.calls
    assert [event["event"] for event in manifest.events] == ["purge_intent", "purged"]
    assert repo.messages[message_id]["local_state"] == "purged"
    assert message_id not in repo.contents


def test_recover_incomplete_purge_finishes_database_update_when_purged_already_written() -> None:
    """A crash after the ``purged`` fsync but before the DB commit must not re-touch storage."""
    repo = InMemoryMessageRepository()
    message_id = _message(repo, uid=1, path="message.eml", raw=b"message", local_state="trashed")
    timestamp = "2026-08-27T00:00:00+00:00"
    intent_and_completion: list[Mapping[str, JSONValue]] = [
        {
            "event": "purge_intent",
            "account_id": "account",
            "source_item_key": "10:1",
            "relative_path": "message.eml",
            "file_hash": hashlib.sha256(b"message").hexdigest(),
            "timestamp": timestamp,
            "shared_reference_count": 0,
            "physical_delete": True,
        },
        {
            "event": "purged",
            "account_id": "account",
            "source_item_key": "10:1",
            "relative_path": "message.eml",
            "file_hash": hashlib.sha256(b"message").hexdigest(),
            "timestamp": timestamp,
            "shared_reference_count": 0,
            "physical_delete": True,
        },
    ]
    manifest = MemoryManifest(intent_and_completion)
    storage = MemoryPurgeStorage({"message.eml": b"message"})

    recover_incomplete_purges(repo, storage, manifest, storage_state=AttachedState())

    assert storage.calls == []  # already durably deleted; recovery must only finish the DB write
    assert repo.messages[message_id]["local_state"] == "purged"
    assert message_id not in repo.contents
    assert len(repo.audit_log) == 1


def test_purge_is_idempotent_when_run_twice_on_the_same_message() -> None:
    repo = InMemoryMessageRepository()
    message_id = _message(repo, uid=1, path="message.eml", raw=b"message", local_state="trashed")
    storage = MemoryPurgeStorage({"message.eml": b"message"})
    manifest = MemoryManifest()

    first = purge(repo, storage, manifest, message_ids=[message_id], storage_state=AttachedState())
    second = purge(repo, storage, manifest, message_ids=[message_id], storage_state=AttachedState())

    assert first.purged_ids == (message_id,)
    assert second.purged_ids == ()
    assert second.skipped_ids == (message_id,)
    assert len(repo.audit_log) == 1
    assert [event["event"] for event in manifest.events] == ["purge_intent", "purged"]
