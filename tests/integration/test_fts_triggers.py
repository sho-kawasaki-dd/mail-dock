import sqlite3
from pathlib import Path

from mail_dock.infrastructure.database.migrator import migrate


def test_fts_triggers_sync_insert_update_and_delete(
    db_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "metadata.db"
    migrate(db_conn, db_path)
    db_conn.execute(
        "INSERT INTO accounts (id, provider_type) VALUES (?, ?)",
        ("account-1", "imap"),
    )
    folder = db_conn.execute(
        "INSERT INTO folders (account_id, raw_name, display_name) VALUES (?, ?, ?)",
        ("account-1", "INBOX", "Inbox"),
    )
    message = db_conn.execute(
        """
        INSERT INTO messages (
            account_id, folder_id, content_key, source_item_key
        ) VALUES (?, ?, ?, ?)
        """,
        ("account-1", folder.lastrowid, "content-1", "source-1"),
    )
    message_id = message.lastrowid
    db_conn.execute(
        """
        INSERT INTO message_contents (
            message_id, subject_norm, sender_norm, body_text, attachment_names
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (message_id, "subject", "sender", "日本語の本文です", "attachment.txt"),
    )
    db_conn.commit()

    def rows_for(term: str) -> list[tuple[int]]:
        return db_conn.execute(
            "SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?",
            (term,),
        ).fetchall()

    assert rows_for("本文で") == [(message_id,)]

    db_conn.execute(
        "UPDATE message_contents SET body_text = ? WHERE message_id = ?",
        ("更新された内容です", message_id),
    )
    db_conn.commit()
    assert rows_for("本文で") == []
    assert rows_for("された") == [(message_id,)]

    db_conn.execute("DELETE FROM message_contents WHERE message_id = ?", (message_id,))
    db_conn.commit()
    assert rows_for("された") == []
