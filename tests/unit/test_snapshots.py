from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime

from mail_dock.domain.fetcher import RemoteFolder
from mail_dock.domain.ports import (
    BaseCredentialStore,
    BaseManifestReader,
    BaseManifestWriter,
    JSONValue,
)
from mail_dock.usecases.register_account import register_account, update_account
from mail_dock.usecases.snapshots import backfill_snapshots, repair_manifest_tails
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
        return iter(())


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


class _Credentials(BaseCredentialStore):
    def set_password(self, account_id: str, password: str) -> None:
        del account_id, password

    def get_password(self, account_id: str) -> str | None:
        del account_id
        return None

    def delete_password(self, account_id: str) -> None:
        del account_id
