from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from mail_dock.domain.errors import StorageDetachedError
from mail_dock.domain.fetcher import CancelToken, RemoteFolder, RemoteMessageRef
from mail_dock.infrastructure.database.connection import connect
from mail_dock.infrastructure.database.message_repository import SqliteMessageRepository
from mail_dock.infrastructure.database.migrator import migrate
from mail_dock.infrastructure.storage.eml_storage import EmlStorage
from mail_dock.infrastructure.storage.manifest import ManifestReader, ManifestWriter, read_events
from mail_dock.usecases.sync_mail import SyncOptions, sync_account
from mail_dock.usecases.verify import verify_manifest
from tests.support.fake_fetcher import FakeFetcher
from tests.support.fault_injection import (
    FaultInjectingConnection,
    FaultInjectingEmlStorage,
    FaultInjector,
    PartialWriteManifest,
    fail_before_eml_fsync,
    fail_before_eml_replace,
)

ACCOUNT_ID = "account"
FOLDER_NAME = "INBOX"
UIDVALIDITY = 41


def _eml() -> bytes:
    return (
        b"From: sender@example.test\r\n"
        b"To: recipient@example.test\r\n"
        b"Subject: Fault injection\r\n"
        b"Message-ID: <fault@example.test>\r\n"
        b"Date: Thu, 30 Jul 2026 12:00:00 +0000\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"fault-injection body\r\n"
    )


def _open_repository(db_path: Path) -> tuple[SqliteMessageRepository, sqlite3.Connection, int]:
    connection = connect(db_path)
    migrate(connection, db_path)
    repository = SqliteMessageRepository(connection)
    repository.upsert_account(
        {
            "id": ACCOUNT_ID,
            "provider_type": "imap",
            "host": "imap.example.test",
            "port": 993,
            "username": "user@example.test",
        }
    )
    folder_id = repository.upsert_folder(
        {
            "account_id": ACCOUNT_ID,
            "raw_name": FOLDER_NAME,
            "display_name": "Inbox",
            "uidvalidity": UIDVALIDITY,
            "is_sync_target": 1,
        }
    )
    return repository, connection, folder_id


def _fetcher(raw: bytes) -> FakeFetcher:
    return FakeFetcher(
        folders=(RemoteFolder(FOLDER_NAME, "Inbox", UIDVALIDITY),),
        messages={
            FOLDER_NAME: [
                RemoteMessageRef(
                    uid=1,
                    internal_date=datetime(2026, 7, 30, tzinfo=UTC),
                    size_bytes=len(raw),
                )
            ]
        },
        eml_bytes={(FOLDER_NAME, 1): raw},
    )


def _sync(
    repository: SqliteMessageRepository,
    storage: FaultInjectingEmlStorage,
    manifest: Any,
    raw: bytes,
) -> Any:
    return sync_account(
        _fetcher(raw),
        repository,
        storage,
        manifest,
        account_id=ACCOUNT_ID,
        options=SyncOptions(),
        cancel=CancelToken(),
    )


def _stored_rows(connection: sqlite3.Connection) -> list[tuple[str | None, str | None, int]]:
    rows = connection.execute(
        "SELECT relative_path, file_hash, size_bytes FROM messages ORDER BY id"
    ).fetchall()
    return [
        (cast(str | None, path), cast(str | None, file_hash), int(size))
        for path, file_hash, size in rows
    ]


def _assert_no_dangling_database_files(connection: sqlite3.Connection, root: Path) -> None:
    for relative_path, file_hash, size_bytes in _stored_rows(connection):
        assert relative_path is not None
        path = root / relative_path
        assert path.is_file()
        raw = path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == file_hash
        assert len(raw) == size_bytes


@pytest.mark.parametrize(
    ("fault_context", "operation"),
    [
        (fail_before_eml_fsync, "eml.fsync"),
        (fail_before_eml_replace, "eml.replace"),
    ],
)
def test_eml_faults_leave_only_unregistered_storage_and_resume(
    tmp_path: Path, fault_context: Any, operation: str
) -> None:
    root = tmp_path / "storage"
    db_path = tmp_path / "metadata.db"
    raw = _eml()
    injector = FaultInjector(operation)
    repository, connection, _ = _open_repository(db_path)
    storage = FaultInjectingEmlStorage(EmlStorage(root), injector)
    manifest = ManifestWriter(root, ACCOUNT_ID)

    try:
        result = None
        with fault_context(injector):
            result = _sync(repository, storage, manifest, raw)
        assert result is not None
        assert result.failed_count == 1
        assert injector.calls[operation] >= 1
        _assert_no_dangling_database_files(connection, root)
        assert _stored_rows(connection) == []
    finally:
        manifest.close()
        connection.close()

    repository, connection, _ = _open_repository(db_path)
    recovery_manifest = ManifestWriter(root, ACCOUNT_ID)
    try:
        recovery = _sync(
            repository,
            FaultInjectingEmlStorage(EmlStorage(root), FaultInjector("unused")),
            recovery_manifest,
            raw,
        )
        assert recovery.fetched_count == 1
        assert len(_stored_rows(connection)) == 1
        _assert_no_dangling_database_files(connection, root)
    finally:
        recovery_manifest.close()
        connection.close()


def test_partial_manifest_record_is_repaired_and_sync_resumes(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    db_path = tmp_path / "metadata.db"
    raw = _eml()
    injector = FaultInjector("manifest.append")
    repository, connection, _ = _open_repository(db_path)
    storage = FaultInjectingEmlStorage(EmlStorage(root), injector)
    manifest = PartialWriteManifest(root, ACCOUNT_ID, injector)
    manifest.checkpoint(1, "prior-batch")

    try:
        with pytest.raises(StorageDetachedError):
            _sync(repository, storage, manifest, raw)
        assert injector.calls["manifest.append"] == 1
        assert _stored_rows(connection) == []
        assert len(tuple((root / "eml").rglob("*.eml"))) == 1
    finally:
        manifest.close()
        connection.close()

    verification = verify_manifest(root)
    assert verification.files_checked == 1
    assert verification.records_checked == 1
    assert verification.repaired_count == 1
    manifest_path = next((root / "manifests" / "imap" / ACCOUNT_ID).glob("events-*.jsonl"))
    assert [event["event"] for event in read_events(manifest_path)] == ["checkpoint"]

    repository, connection, _ = _open_repository(db_path)
    recovery_manifest = ManifestWriter(root, ACCOUNT_ID)
    try:
        recovery = _sync(
            repository,
            FaultInjectingEmlStorage(EmlStorage(root), FaultInjector("unused")),
            recovery_manifest,
            raw,
        )
        assert recovery.fetched_count == 1
        assert len(_stored_rows(connection)) == 1
        _assert_no_dangling_database_files(connection, root)
    finally:
        recovery_manifest.close()
        connection.close()


def test_db_commit_fault_has_no_checkpoint_and_is_next_range_target(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    db_path = tmp_path / "metadata.db"
    raw = _eml()
    injector = FaultInjector("db.commit", occurrence=2)
    repository, connection, _ = _open_repository(db_path)
    cast(Any, repository)._connection = FaultInjectingConnection(connection, injector)
    storage = FaultInjectingEmlStorage(EmlStorage(root), injector)
    manifest = ManifestWriter(root, ACCOUNT_ID)

    try:
        with pytest.raises(StorageDetachedError):
            _sync(repository, storage, manifest, raw)
        assert injector.calls["db.commit"] == 2
    finally:
        manifest.close()
        connection.close()

    repository, connection, _ = _open_repository(db_path)
    try:
        assert _stored_rows(connection) == []
    finally:
        connection.close()

    repository, connection, _ = _open_repository(db_path)
    reader = ManifestReader(root, ACCOUNT_ID)
    pending_events = list(reader.read_events_since_checkpoint())
    assert [event["event"] for event in pending_events] == ["fetch"]
    assert list(reader.read_all_events())[-1]["event"] == "fetch"
    _assert_no_dangling_database_files(connection, root)

    recovery_manifest = ManifestWriter(root, ACCOUNT_ID)
    try:
        recovery = _sync(
            repository,
            FaultInjectingEmlStorage(EmlStorage(root), FaultInjector("unused")),
            recovery_manifest,
            raw,
        )
        assert recovery.fetched_count == 1
        assert len(_stored_rows(connection)) == 1
        _assert_no_dangling_database_files(connection, root)
        events = list(ManifestReader(root, ACCOUNT_ID).read_all_events())
        assert [event["event"] for event in events].count("checkpoint") == 1
    finally:
        recovery_manifest.close()
        connection.close()
