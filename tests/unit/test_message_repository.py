from __future__ import annotations

import sqlite3
from pathlib import Path

from mail_dock.infrastructure.database.connection import connect
from mail_dock.infrastructure.database.message_repository import SqliteMessageRepository
from mail_dock.infrastructure.database.migrator import migrate


def _repository(connection: sqlite3.Connection, db_path: Path) -> tuple[SqliteMessageRepository, int]:
    migrate(connection, db_path)
    repository = SqliteMessageRepository(connection)
    repository.upsert_account({"id": "account", "provider_type": "imap"})
    folder_id = repository.upsert_folder(
        {"account_id": "account", "raw_name": "INBOX", "display_name": "Inbox"}
    )
    return repository, folder_id


def _message(folder_id: int, uidvalidity: int, uid: int = 7) -> dict[str, object]:
    return {
        "account_id": "account",
        "folder_id": folder_id,
        "uid": uid,
        "uidvalidity": uidvalidity,
        "content_key": f"message:{uidvalidity}:{uid}",
        "source_item_key": f"{uidvalidity}:{uid}",
        "subject": f"Subject {uidvalidity}",
        "relative_path": f"eml/{uidvalidity}-{uid}.eml",
        "file_hash": f"hash-{uidvalidity}-{uid}",
        "size_bytes": 10,
    }


def test_add_message_separates_uid_generations_and_normalizes_contents(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    repository, folder_id = _repository(db_conn, tmp_path / "metadata.db")

    repository.begin_batch()
    first_id = repository.add_message(
        _message(folder_id, 11),
        {"subject": "ＦＯＯ", "body_text": "Hello   WORLD"},
    )
    second_id = repository.add_message(_message(folder_id, 12))
    assert repository.add_message(_message(folder_id, 11)) == first_id
    repository.commit_batch()

    assert first_id != second_id
    assert repository.local_uids("account", folder_id, 11) == {7}
    assert repository.local_uids("account", folder_id, 12) == {7}
    contents = db_conn.execute("SELECT subject_norm, body_text FROM message_contents").fetchone()
    assert contents == ("foo", "hello world")
    assert db_conn.execute("SELECT COUNT(*) FROM messages").fetchone() == (2,)


def test_batch_changes_are_not_committed_per_message(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    repository, folder_id = _repository(db_conn, tmp_path / "metadata.db")
    repository.begin_batch()
    repository.add_message(_message(folder_id, 11))

    other_connection = connect(tmp_path / "metadata.db")
    try:
        assert other_connection.execute("SELECT COUNT(*) FROM messages").fetchone() == (0,)
    finally:
        other_connection.close()

    repository.commit_batch()
    assert db_conn.execute("SELECT COUNT(*) FROM messages").fetchone() == (1,)


def test_failures_are_upserted_and_filtered_by_uid_generation(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    repository, folder_id = _repository(db_conn, tmp_path / "metadata.db")

    repository.record_failure("account", folder_id, 11, 7, "transient", "first")
    repository.record_failure("account", folder_id, 11, 7, "transient", "second")
    repository.record_failure("account", folder_id, 12, 7, "permanent", "other generation")

    current = repository.pending_failures("account", folder_id, 11)
    old = repository.pending_failures("account", folder_id, 12)
    assert len(current) == 1
    assert current[0]["attempt_count"] == 2
    assert old[0]["error_class"] == "permanent"

    repository.clear_failure("account", folder_id, 11, 7)
    assert repository.pending_failures("account", folder_id, 11) == []
    assert len(repository.pending_failures("account", folder_id, 12)) == 1