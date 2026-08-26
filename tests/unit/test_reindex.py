from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime
from pathlib import Path

import pytest

from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.messages import StoredEml
from mail_dock.domain.ports import BaseEmlStorage, BaseManifestReader, JSONValue
from mail_dock.infrastructure.database.connection import connect
from mail_dock.infrastructure.database.message_repository import SqliteMessageRepository
from mail_dock.infrastructure.database.migrator import migrate
from mail_dock.infrastructure.database.reindex import rebuild_database
from mail_dock.infrastructure.storage.eml_storage import EmlStorage
from mail_dock.infrastructure.storage.manifest import ManifestReader, ManifestWriter
from mail_dock.infrastructure.storage.storage_root import initialize_root
from mail_dock.usecases.reindex import ReindexProgress, reindex
from tests.support.in_memory_repository import InMemoryMessageRepository


class MemoryManifestReader(BaseManifestReader):
    def __init__(self, events: Sequence[Mapping[str, JSONValue]]) -> None:
        self.events = tuple(events)

    def read_all_events(self) -> Iterator[Mapping[str, JSONValue]]:
        yield from self.events

    def read_last_checkpoint(self) -> Mapping[str, JSONValue] | None:
        return None

    def read_events_since_checkpoint(self) -> Iterator[Mapping[str, JSONValue]]:
        yield from self.events

    def read_incomplete_intents(self) -> Iterator[Mapping[str, JSONValue]]:
        return iter(())


class MemoryEmlStorage(BaseEmlStorage):
    def __init__(self, files: Mapping[str, bytes]) -> None:
        self.files = dict(files)

    def save(self, account_id: str, internal_date: datetime | None, raw: bytes) -> StoredEml:
        del account_id, internal_date, raw
        raise NotImplementedError

    def reuse(self, relative_path: str, expected_hash: str) -> StoredEml | None:
        del relative_path, expected_hash
        raise NotImplementedError

    def read(self, relative_path: str) -> bytes:
        try:
            return self.files[relative_path]
        except KeyError as error:
            raise FileNotFoundError(relative_path) from error

    def read_verified(self, relative_path: str, expected_hash: str) -> bytes:
        raw = self.read(relative_path)
        if hashlib.sha256(raw).hexdigest() != expected_hash:
            raise ValueError("hash mismatch")
        return raw


def _raw(subject: str = "Reindexed") -> bytes:
    return (
        f"From: sender@example.com\nSubject: {subject}\n"
        "Message-ID: <reindexed@example.com>\n"
        "Content-Type: text/plain; charset=utf-8\n\nRebuilt body\n"
    ).encode()


def _base_events(relative_path: str, file_hash: str) -> list[dict[str, JSONValue]]:
    return [
        {
            "event": "account_snapshot",
            "account_id": "account",
            "provider_type": "onamae_imap",
            "display_name": "Account",
            "host": "imap.example.com",
            "port": 993,
            "username": "user@example.com",
            "timestamp": "2026-01-01T00:00:00+00:00",
        },
        {
            "event": "folder_snapshot",
            "account_id": "account",
            "folder_raw_name": "INBOX",
            "display_name": "Inbox",
            "uidvalidity": 42,
            "delimiter": "/",
            "is_sync_target": True,
            "timestamp": "2026-01-01T00:00:00+00:00",
        },
        {
            "event": "fetch",
            "account_id": "account",
            "folder_raw_name": "INBOX",
            "uid": 7,
            "uidvalidity": 42,
            "source_item_key": "42:7",
            "message_id": "<reindexed@example.com>",
            "relative_path": relative_path,
            "file_hash": file_hash,
            "size_bytes": len(_raw()),
            "internal_date": "2026-01-02T03:04:05+00:00",
            "timestamp": "2026-01-02T03:04:05+00:00",
            "deduplicated": False,
        },
    ]


def test_reindex_restores_snapshots_and_reparses_eml() -> None:
    raw = _raw()
    relative_path = "eml/account/2026/01/message.eml"
    repository = InMemoryMessageRepository()
    progress: list[ReindexProgress] = []

    result = reindex(
        repository,
        MemoryEmlStorage({relative_path: raw}),
        MemoryManifestReader(_base_events(relative_path, hashlib.sha256(raw).hexdigest())),
        cancel=CancelToken(),
        on_progress=progress.append,
    )

    assert result.account_count == 1
    assert result.folder_count == 1
    assert result.message_count == 1
    assert result.contents_count == 1
    assert result.skipped_count == 0
    assert repository.folders[1]["is_sync_target"] == 0
    assert repository.messages[1]["source_item_key"] == "42:7"
    assert repository.contents[1]["subject_norm"] == "Reindexed"
    assert repository.contents[1]["body_text"] == "Rebuilt body\n"
    assert progress == [ReindexProgress(1, 1, relative_path)]


def test_reindex_restores_remote_state_and_purged_tombstone_without_eml() -> None:
    raw = _raw()
    relative_path = "eml/account/2026/01/message.eml"
    file_hash = hashlib.sha256(raw).hexdigest()
    events = _base_events(relative_path, file_hash)
    events.extend(
        [
            {
                "event": "remote_delete_uncertain",
                "account_id": "account",
                "folder_raw_name": "INBOX",
                "uid": 7,
                "uidvalidity": 42,
                "mode": "expunge",
                "timestamp": "2026-01-03T00:00:00+00:00",
            },
            {
                "event": "purge_intent",
                "account_id": "account",
                "source_item_key": "42:7",
                "relative_path": relative_path,
                "file_hash": file_hash,
                "shared_reference_count": 0,
                "physical_delete": True,
                "timestamp": "2026-01-04T00:00:00+00:00",
            },
            {
                "event": "purged",
                "account_id": "account",
                "source_item_key": "42:7",
                "relative_path": relative_path,
                "file_hash": file_hash,
                "shared_reference_count": 0,
                "physical_delete": True,
                "timestamp": "2026-01-04T00:00:01+00:00",
            },
        ]
    )

    repository = InMemoryMessageRepository()
    result = reindex(
        repository,
        MemoryEmlStorage({}),
        MemoryManifestReader(events),
    )

    assert result.purged_count == 1
    assert repository.messages[1]["remote_state"] == "uncertain"
    assert repository.messages[1]["local_state"] == "purged"
    assert repository.messages[1]["relative_path"] is None
    assert repository.messages[1]["file_hash"] == file_hash
    assert 1 not in repository.contents
    assert result.contents_count == 0
    assert any(entry["operation"] == "local_purge" for entry in repository.audit_log)


def test_reindex_restores_remote_delete_audit_metadata() -> None:
    raw = _raw()
    relative_path = "eml/account/2026/01/message.eml"
    file_hash = hashlib.sha256(raw).hexdigest()
    events = _base_events(relative_path, file_hash)
    events.append(
        {
            "event": "remote_delete_completed",
            "account_id": "account",
            "folder_raw_name": "INBOX",
            "uid": 7,
            "uidvalidity": 42,
            "mode": "trash",
            "timestamp": "2026-01-03T00:00:00+00:00",
        }
    )

    repository = InMemoryMessageRepository()
    reindex(repository, MemoryEmlStorage({relative_path: raw}), MemoryManifestReader(events))

    audit_entry = next(
        entry for entry in repository.audit_log if entry["operation"] == "remote_delete"
    )
    assert audit_entry["message_id"] == "<reindexed@example.com>"
    assert audit_entry["subject"] == "Reindexed"
    assert audit_entry["size_bytes"] == len(raw)


def test_rebuild_database_warns_and_skips_pst_manifests(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "storage"
    initialize_root(root)
    (root / "manifests" / "pst" / "import-1").mkdir(parents=True)
    caplog.set_level(logging.WARNING, logger="mail_dock.infrastructure.database.reindex")

    result = rebuild_database(
        root / "metadata.db",
        EmlStorage(root),
        [],
    )

    assert result.message_count == 0
    assert "Skipping unsupported PST manifests" in caplog.text


def test_rebuild_database_replaces_existing_database_only_after_verification(
    tmp_path: Path,
) -> None:
    root = tmp_path / "storage"
    initialize_root(root)
    database_path = root / "metadata.db"
    old_connection = connect(database_path)
    migrate(old_connection, database_path)
    old_repository = SqliteMessageRepository(old_connection)
    old_repository.upsert_account(
        {
            "id": "old-account",
            "provider_type": "onamae_imap",
            "display_name": "Old",
            "host": "old.example.com",
            "port": 993,
            "username": "old@example.com",
        }
    )
    old_connection.close()

    raw = _raw("Replacement")
    storage = EmlStorage(root)
    stored = storage.save("account", datetime(2026, 1, 2), raw)
    writer = ManifestWriter(root, "account")
    writer.append(
        {
            "event": "account_snapshot",
            "account_id": "account",
            "provider_type": "onamae_imap",
            "display_name": "Account",
            "host": "imap.example.com",
            "port": 993,
            "username": "user@example.com",
            "timestamp": "2026-01-01T00:00:00+00:00",
        }
    )
    writer.append(
        {
            "event": "folder_snapshot",
            "account_id": "account",
            "folder_raw_name": "INBOX",
            "display_name": "Inbox",
            "uidvalidity": 42,
            "delimiter": "/",
            "timestamp": "2026-01-01T00:00:00+00:00",
        }
    )
    writer.append(
        {
            "event": "fetch",
            "account_id": "account",
            "folder_raw_name": "INBOX",
            "uid": 7,
            "uidvalidity": 42,
            "source_item_key": "42:7",
            "message_id": "<reindexed@example.com>",
            "relative_path": stored.relative_path,
            "file_hash": stored.file_hash,
            "size_bytes": stored.size_bytes,
            "internal_date": "2026-01-02T00:00:00+00:00",
            "timestamp": "2026-01-02T00:00:00+00:00",
            "deduplicated": False,
        }
    )
    writer.close()

    result = rebuild_database(
        database_path,
        storage,
        [ManifestReader(root, "account")],
    )

    assert result.message_count == 1
    connection = connect(database_path)
    try:
        accounts = connection.execute("SELECT id FROM accounts ORDER BY id").fetchall()
        messages = connection.execute("SELECT source_item_key FROM messages").fetchall()
        assert accounts == [("account",)]
        assert messages == [("42:7",)]
    finally:
        connection.close()
