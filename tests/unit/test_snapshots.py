from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Any

from mail_dock.domain.fetcher import RemoteFolder
from mail_dock.domain.ports import (
    BaseCredentialStore,
    BaseIntegrityStorage,
    BaseManifestReader,
    BaseManifestWriter,
    BasePurgeStorage,
    JSONValue,
)
from mail_dock.usecases.register_account import register_account, update_account
from mail_dock.usecases.snapshots import (
    backfill_snapshots,
    recover_after_unclean_shutdown,
    repair_manifest_tails,
)
from mail_dock.usecases.sync_folders import refresh_folders, set_sync_target
from tests.support.fake_fetcher import FakeFetcher
from tests.support.in_memory_repository import InMemoryMessageRepository


class MemoryManifest(BaseManifestWriter, BaseManifestReader):
    def __init__(self) -> None:
        self.events: list[Mapping[str, JSONValue]] = []
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
                "timestamp": datetime.now(UTC).isoformat(),
                "sequence": sequence,
                "batch_id": batch_id,
            }
        )

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


def test_account_snapshot_skips_unchanged_state_and_records_changes() -> None:
    repository = InMemoryMessageRepository()
    credentials = _Credentials()
    manifest = MemoryManifest()

    register_account(
        repository,
        credentials,
        account_id="account",
        host="imap.example.test",
        port=993,
        username="user",
        password="secret",
        display_name=None,
        manifest=manifest,
        manifest_reader=manifest,
    )
    update_account(
        repository,
        credentials,
        account_id="account",
        host="imap.example.test",
        port=993,
        username="user",
        password=None,
        display_name=None,
        is_enabled=True,
        manifest=manifest,
        manifest_reader=manifest,
    )
    update_account(
        repository,
        credentials,
        account_id="account",
        host="imap.changed.test",
        port=993,
        username="user",
        password=None,
        display_name=None,
        is_enabled=True,
        manifest=manifest,
        manifest_reader=manifest,
    )

    snapshots = [event for event in manifest.events if event.get("event") == "account_snapshot"]
    assert len(snapshots) == 2
    assert snapshots[-1]["host"] == "imap.changed.test"


def test_folder_snapshot_tracks_sync_target_changes_without_duplicates() -> None:
    repository = InMemoryMessageRepository()
    manifest = MemoryManifest()
    fetcher = FakeFetcher(folders=(RemoteFolder("INBOX", "Inbox", uidvalidity=7, delimiter="/"),))

    refresh_folders(fetcher, repository, "account", manifest=manifest, manifest_reader=manifest)
    refresh_folders(fetcher, repository, "account", manifest=manifest, manifest_reader=manifest)
    set_sync_target(
        repository,
        "account",
        "INBOX",
        True,
        manifest=manifest,
        manifest_reader=manifest,
    )

    snapshots = [event for event in manifest.events if event.get("event") == "folder_snapshot"]
    assert len(snapshots) == 2
    assert snapshots[0]["is_sync_target"] is False
    assert snapshots[1]["is_sync_target"] is True


def test_backfill_records_each_existing_account_and_folder_once() -> None:
    repository = InMemoryMessageRepository()
    repository.upsert_account(
        {
            "id": "account",
            "provider_type": "onamae_imap",
            "display_name": "Example",
            "host": "imap.example.test",
            "port": 993,
            "username": "user",
        }
    )
    repository.upsert_folder(
        {
            "account_id": "account",
            "raw_name": "INBOX",
            "display_name": "Inbox",
            "uidvalidity": 7,
        }
    )
    manifests: dict[str, MemoryManifest] = {}

    def writer_factory(account_id: str) -> MemoryManifest:
        return manifests.setdefault(account_id, MemoryManifest())

    first = backfill_snapshots(repository, writer_factory, writer_factory)
    second = backfill_snapshots(repository, writer_factory, writer_factory)

    assert first == (1, 1)
    assert second == (0, 0)
    assert len(manifests["account"].events) == 2


def test_repair_manifest_tails_reads_each_existing_account_manifest() -> None:
    repository = InMemoryMessageRepository()
    repository.upsert_account({"id": "account"})
    manifest = MemoryManifest()
    manifest.events.append(
        {
            "event": "account_snapshot",
            "account_id": "account",
        }
    )

    assert repair_manifest_tails(repository, lambda _account_id: manifest) == 1


class MemoryIntegrityAndPurgeStorage(BaseIntegrityStorage, BasePurgeStorage):
    def __init__(self, files: Mapping[str, bytes]) -> None:
        self.files = dict(files)

    def stat(self, relative_path: str) -> Any:
        if relative_path not in self.files:
            raise FileNotFoundError(relative_path)

        class FileStat:
            st_size = len(self.files[relative_path])

        return FileStat()

    def iter_chunks(self, relative_path: str, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        payload = self.files[relative_path]
        for start in range(0, len(payload), chunk_size):
            yield payload[start : start + chunk_size]

    def iter_eml_paths(self, account_id: str | None = None) -> Iterator[str]:
        del account_id
        yield from sorted(self.files)

    def quarantine(self, relative_path: str) -> None:
        self.files.pop(relative_path, None)

    def exists(self, relative_path: str) -> bool:
        return relative_path in self.files

    def delete(self, relative_path: str) -> None:
        self.files.pop(relative_path, None)


class _AttachedState:
    def is_write_allowed(self) -> bool:
        return True


def test_recover_after_unclean_shutdown_range_verifies_and_resumes_purges() -> None:
    repository = InMemoryMessageRepository()
    repository.upsert_account({"id": "account"})
    raw = b"hello"
    file_hash = hashlib.sha256(raw).hexdigest()
    message_id = repository.add_message(
        {
            "account_id": "account",
            "folder_id": 1,
            "uid": 1,
            "uidvalidity": 10,
            "source_item_key": "10:1",
            "relative_path": "message.eml",
            "file_hash": file_hash,
            "size_bytes": len(raw),
            "local_state": "trashed",
        }
    )
    manifest = MemoryManifest()
    manifest.events.append(
        {
            "event": "fetch",
            "account_id": "account",
            "folder_raw_name": "INBOX",
            "uid": 1,
            "uidvalidity": 10,
            "source_item_key": "10:1",
            "message_id": None,
            "relative_path": "message.eml",
            "file_hash": file_hash,
            "size_bytes": len(raw),
            "internal_date": None,
            "timestamp": "2026-08-26T00:00:00Z",
            "deduplicated": False,
        }
    )
    manifest.events.append(
        {
            "event": "purge_intent",
            "account_id": "account",
            "source_item_key": "10:1",
            "relative_path": "message.eml",
            "file_hash": file_hash,
            "timestamp": "2026-08-26T00:00:01Z",
            "shared_reference_count": 0,
            "physical_delete": True,
        }
    )
    storage = MemoryIntegrityAndPurgeStorage({"message.eml": raw})

    results = recover_after_unclean_shutdown(
        repository,
        storage,
        storage,
        lambda _account_id: manifest,
        lambda _account_id: manifest,
        storage_state=_AttachedState(),
    )

    assert len(results) == 1
    assert results[0].checked_count == 1
    assert repository.messages[message_id]["local_state"] == "purged"
    assert "message.eml" not in storage.files
    assert [event["event"] for event in manifest.events] == ["fetch", "purge_intent", "purged"]


class _Credentials(BaseCredentialStore):
    def set_password(self, account_id: str, password: str) -> None:
        del account_id, password

    def get_password(self, account_id: str) -> str | None:
        del account_id
        return None

    def delete_password(self, account_id: str) -> None:
        del account_id
