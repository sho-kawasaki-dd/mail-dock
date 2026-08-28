from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

import pytest

from mail_dock.domain.fetcher import CancelToken
from mail_dock.infrastructure.database.connection import connect
from mail_dock.infrastructure.database.message_repository import SqliteMessageRepository
from mail_dock.infrastructure.database.migrator import migrate
from mail_dock.infrastructure.database.reindex import rebuild_database
from mail_dock.infrastructure.storage.eml_storage import EmlStorage
from mail_dock.infrastructure.storage.manifest import ManifestReader, ManifestWriter
from mail_dock.infrastructure.storage.storage_root import initialize_root
from mail_dock.usecases.sync_mail import SyncOptions, sync_account
from tests.support.imap_integration import (
    append_message,
    create_mailbox,
    imap_client,
    make_fetcher,
    open_repository,
    register_account_and_folder,
    service,
    unique_mailbox,
)


def _cache_snapshot(connection: sqlite3.Connection) -> dict[str, object]:
    accounts = connection.execute(
        "SELECT id, provider_type, display_name, host, port, username, is_enabled "
        "FROM accounts ORDER BY id"
    ).fetchall()
    folders = connection.execute(
        "SELECT account_id, raw_name, display_name, uidvalidity FROM folders "
        "ORDER BY account_id, raw_name"
    ).fetchall()
    messages = connection.execute(
        "SELECT m.account_id, f.raw_name, m.message_id, m.content_key, "
        "m.source_item_key, m.uid, m.uidvalidity, m.remote_state, m.local_state, "
        "m.relative_path, m.file_hash, m.subject, m.sender, m.recipient, m.cc, "
        "m.date_sent, m.internal_date, m.size_bytes, m.has_attachment, "
        "m.in_reply_to, m.references_ids, m.thread_key "
        "FROM messages AS m JOIN folders AS f ON f.id = m.folder_id "
        "ORDER BY m.account_id, f.raw_name, m.uidvalidity, m.uid"
    ).fetchall()
    contents = connection.execute(
        "SELECT f.raw_name, m.source_item_key, c.subject_norm, c.sender_norm, "
        "c.body_text, c.attachment_names "
        "FROM message_contents AS c "
        "JOIN messages AS m ON m.id = c.message_id "
        "JOIN folders AS f ON f.id = m.folder_id "
        "ORDER BY f.raw_name, m.source_item_key"
    ).fetchall()
    fts_results = connection.execute(
        "SELECT m.source_item_key FROM messages_fts AS f "
        "JOIN messages AS m ON m.id = f.rowid "
        "WHERE messages_fts MATCH ? ORDER BY m.source_item_key",
        ("reindex-marker",),
    ).fetchall()
    return {
        "accounts": accounts,
        "folders": folders,
        "messages": messages,
        "contents": contents,
        "fts": fts_results,
    }


def _append_fetch(
    writer: ManifestWriter,
    *,
    uid: int,
    relative_path: str,
    file_hash: str,
    size_bytes: int,
    message_id: str,
) -> None:
    writer.append(
        {
            "event": "fetch",
            "account_id": "account",
            "folder_raw_name": "INBOX",
            "uid": uid,
            "uidvalidity": 42,
            "source_item_key": f"42:{uid}",
            "message_id": message_id,
            "relative_path": relative_path,
            "file_hash": file_hash,
            "size_bytes": size_bytes,
            "internal_date": "2026-01-02T03:04:05+00:00",
            "timestamp": "2026-01-02T03:04:05+00:00",
            "deduplicated": False,
        }
    )


@pytest.mark.docker
def test_reindex_after_real_sync_preserves_the_semantic_cache(tmp_path: Path) -> None:
    settings = service("dovecot")
    mailbox = unique_mailbox("ReindexFlow")
    with imap_client(settings) as client:
        create_mailbox(client, mailbox)
        append_message(
            client,
            mailbox,
            subject="reindex-marker",
            body="reindex body marker",
        )

    account_id = "integration-reindex"
    repository, connection = open_repository(tmp_path)
    root = tmp_path / "storage"
    initialize_root(root)
    register_account_and_folder(repository, account_id, mailbox)
    storage = EmlStorage(root)
    manifest = ManifestWriter(root, account_id)
    fetcher = make_fetcher(settings)
    try:
        fetcher.connect()
        manifest.append(
            {
                "event": "account_snapshot",
                "account_id": account_id,
                "provider_type": "imap",
                "display_name": None,
                "host": "integration.test",
                "port": 993,
                "username": settings.username,
                "timestamp": "2026-01-01T00:00:00+00:00",
            }
        )
        from mail_dock.usecases.sync_folders import refresh_folders

        refresh_folders(
            fetcher,
            repository,
            account_id,
            manifest=manifest,
            manifest_reader=ManifestReader(root, account_id),
        )
        result = sync_account(
            fetcher,
            repository,
            storage,
            manifest,
            account_id=account_id,
            options=SyncOptions(),
            cancel=CancelToken(),
        )
    finally:
        fetcher.disconnect()
        manifest.close()

    assert result.fetched_count == 1
    before = _cache_snapshot(connection)
    database_path = tmp_path / "metadata.db"
    connection.close()
    database_path.unlink()

    rebuild_result = rebuild_database(
        database_path,
        storage,
        [ManifestReader(root, account_id)],
    )

    assert rebuild_result.message_count == 1
    rebuilt_connection = connect(database_path)
    try:
        assert _cache_snapshot(rebuilt_connection) == before
        targets = rebuilt_connection.execute(
            "SELECT is_sync_target FROM folders ORDER BY id"
        ).fetchall()
        assert targets
        assert all(row[0] == 0 for row in targets)
    finally:
        rebuilt_connection.close()


def test_reindex_rebuilds_tombstones_remote_states_and_fts(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    initialize_root(root)
    database_path = root / "metadata.db"
    storage = EmlStorage(root)
    connection = connect(database_path)
    migrate(connection, database_path)
    repository = SqliteMessageRepository(connection)
    repository.upsert_account(
        {
            "id": "account",
            "provider_type": "imap",
            "display_name": "Account",
            "host": "imap.example.test",
            "port": 993,
            "username": "user@example.test",
        }
    )
    repository.upsert_folder(
        {
            "account_id": "account",
            "raw_name": "INBOX",
            "display_name": "Inbox",
            "uidvalidity": 42,
            "is_sync_target": 1,
        }
    )

    writer = ManifestWriter(root, "account")
    writer.append(
        {
            "event": "account_snapshot",
            "account_id": "account",
            "provider_type": "imap",
            "display_name": "Account",
            "host": "imap.example.test",
            "port": 993,
            "username": "user@example.test",
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
            "is_sync_target": True,
            "timestamp": "2026-01-01T00:00:00+00:00",
        }
    )
    stored_files: list[tuple[int, str, str]] = []
    for uid, subject in ((1, "reindex-marker"), (2, "completed"), (3, "purged")):
        message = EmailMessage()
        message["From"] = "sender@example.test"
        message["To"] = "recipient@example.test"
        message["Subject"] = subject
        message["Message-ID"] = f"<message-{uid}@example.test>"
        message["Date"] = "Thu, 2 Jan 2026 03:04:05 +0000"
        message.set_content(f"body for {subject}")
        raw = message.as_bytes()
        stored = storage.save("account", datetime(2026, 1, 2, tzinfo=UTC), raw)
        _append_fetch(
            writer,
            uid=uid,
            relative_path=stored.relative_path,
            file_hash=stored.file_hash,
            size_bytes=stored.size_bytes,
            message_id=f"<message-{uid}@example.test>",
        )
        stored_files.append((uid, stored.relative_path, stored.file_hash))

    writer.append(
        {
            "event": "remote_delete_completed",
            "account_id": "account",
            "folder_raw_name": "INBOX",
            "uid": 2,
            "uidvalidity": 42,
            "mode": "trash",
            "timestamp": "2026-01-03T00:00:00+00:00",
        }
    )
    purge_path = stored_files[2][1]
    purge_hash = stored_files[2][2]
    writer.append(
        {
            "event": "remote_delete_uncertain",
            "account_id": "account",
            "folder_raw_name": "INBOX",
            "uid": 3,
            "uidvalidity": 42,
            "mode": "expunge",
            "timestamp": "2026-01-03T00:00:01+00:00",
        }
    )
    writer.append(
        {
            "event": "purge_intent",
            "account_id": "account",
            "source_item_key": "42:3",
            "relative_path": purge_path,
            "file_hash": purge_hash,
            "timestamp": "2026-01-04T00:00:00+00:00",
            "shared_reference_count": 0,
            "physical_delete": True,
        }
    )
    writer.append(
        {
            "event": "purged",
            "account_id": "account",
            "source_item_key": "42:3",
            "relative_path": purge_path,
            "file_hash": purge_hash,
            "timestamp": "2026-01-04T00:00:01+00:00",
            "shared_reference_count": 0,
            "physical_delete": True,
        }
    )
    writer.close()
    (root / purge_path).unlink()
    connection.close()
    database_path.unlink()

    result = rebuild_database(
        database_path,
        storage,
        [ManifestReader(root, "account")],
    )

    assert result.message_count == 3
    assert result.purged_count == 1
    rebuilt_connection = connect(database_path)
    try:
        states = rebuilt_connection.execute(
            "SELECT uid, remote_state, local_state, relative_path FROM messages ORDER BY uid"
        ).fetchall()
        assert states == [
            (1, "present", "active", stored_files[0][1]),
            (2, "deleted", "active", stored_files[1][1]),
            (3, "uncertain", "purged", None),
        ]
        assert rebuilt_connection.execute(
            "SELECT m.source_item_key FROM messages_fts AS f "
            "JOIN messages AS m ON m.id = f.rowid "
            "WHERE messages_fts MATCH ?",
            ('"reindex-marker"',),
        ).fetchall()
        assert rebuilt_connection.execute("SELECT is_sync_target FROM folders").fetchall() == [(0,)]
        audit_operations = rebuilt_connection.execute(
            "SELECT operation FROM audit_log ORDER BY occurred_at, id"
        ).fetchall()
        assert ("remote_delete",) in audit_operations
        assert ("local_purge",) in audit_operations
    finally:
        rebuilt_connection.close()
