import sqlite3
from pathlib import Path

import pytest

from mail_dock.domain.errors import DatabaseError
from mail_dock.infrastructure.database.fts_maintenance import integrity_check
from mail_dock.infrastructure.database.migrator import migrate


def _insert_message_content(connection: sqlite3.Connection) -> int:
    connection.execute(
        "INSERT INTO accounts (id, provider_type) VALUES (?, ?)",
        ("account-1", "imap"),
    )
    folder = connection.execute(
        "INSERT INTO folders (account_id, raw_name, display_name) VALUES (?, ?, ?)",
        ("account-1", "INBOX", "Inbox"),
    )
    message = connection.execute(
        """
        INSERT INTO messages (
            account_id, folder_id, content_key, source_item_key
        ) VALUES (?, ?, ?, ?)
        """,
        ("account-1", folder.lastrowid, "content-1", "source-1"),
    )
    message_id = message.lastrowid
    assert message_id is not None
    connection.execute(
        """
        INSERT INTO message_contents (
            message_id, subject_norm, sender_norm, body_text, attachment_names
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (message_id, "subject", "sender", "body", "attachment.txt"),
    )
    connection.commit()
    return message_id


def test_integrity_check_accepts_consistent_external_content_fts(
    db_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    migrate(db_conn, tmp_path / "metadata.db")
    _insert_message_content(db_conn)

    integrity_check(db_conn)


def test_integrity_check_detects_external_content_fts_divergence(
    db_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    migrate(db_conn, tmp_path / "metadata.db")
    message_id = _insert_message_content(db_conn)
    db_conn.execute(
        "DELETE FROM messages_fts WHERE rowid = ?",
        (message_id,),
    )
    db_conn.commit()

    with pytest.raises(DatabaseError, match="FTS integrity check failed"):
        integrity_check(db_conn)
