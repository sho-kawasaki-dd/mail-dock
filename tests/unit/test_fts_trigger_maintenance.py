from __future__ import annotations

import sqlite3
from pathlib import Path

from mail_dock.infrastructure.database.message_repository import SqliteMessageRepository
from mail_dock.infrastructure.database.migrator import migrate


def test_message_contents_update_and_delete_follow_fts(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    migrate(db_conn, tmp_path / "metadata.db")
    repository = SqliteMessageRepository(db_conn)
    repository.upsert_account({"id": "account", "provider_type": "imap"})
    folder_id = repository.upsert_folder(
        {"account_id": "account", "raw_name": "INBOX", "display_name": "Inbox"}
    )
    message_id = repository.add_message(
        {
            "account_id": "account",
            "folder_id": folder_id,
            "uid": 1,
            "uidvalidity": 1,
            "content_key": "content-1",
            "source_item_key": "source-1",
            "subject": "old subject",
        },
        {"subject": "old subject", "body_text": "old body"},
    )

    assert db_conn.execute(
        "SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?", ('"old subject"',)
    ).fetchall() == [(message_id,)]
    db_conn.execute(
        "UPDATE message_contents SET subject_norm = ?, body_text = ? WHERE message_id = ?",
        ("new subject", "new body", message_id),
    )
    assert db_conn.execute(
        "SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?", ('"new subject"',)
    ).fetchall() == [(message_id,)]
    assert (
        db_conn.execute(
            "SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?", ('"old subject"',)
        ).fetchall()
        == []
    )

    db_conn.execute("DELETE FROM message_contents WHERE message_id = ?", (message_id,))
    assert (
        db_conn.execute(
            "SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?", ('"new subject"',)
        ).fetchall()
        == []
    )
