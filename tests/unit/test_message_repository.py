from __future__ import annotations

import sqlite3
from pathlib import Path

from mail_dock.infrastructure.database.connection import connect
from mail_dock.infrastructure.database.message_repository import SqliteMessageRepository
from mail_dock.infrastructure.database.migrator import migrate


def _repository(
    connection: sqlite3.Connection, db_path: Path
) -> tuple[SqliteMessageRepository, int]:
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
        {"subject": "\uff26\uff2f\uff2f", "body_text": "Hello   WORLD"},
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


def test_add_message_replaces_lone_surrogates_in_contents(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    repository, folder_id = _repository(db_conn, tmp_path / "metadata.db")

    repository.begin_batch()
    repository.add_message(
        _message(folder_id, 11),
        {"subject": "broken\udcff subject", "body_text": "body\ud800 text"},
    )
    repository.commit_batch()

    contents = db_conn.execute("SELECT subject_norm, body_text FROM message_contents").fetchone()
    assert contents == ("broken? subject", "body? text")


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


def test_flag_refresh_repository_operations_and_modseq_reset(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    repository, folder_id = _repository(db_conn, tmp_path / "metadata.db")
    repository.begin_batch()
    repository.add_message(
        {
            **_message(folder_id, 11, uid=1),
            "internal_date": "2026-07-01T00:00:00Z",
            "imap_flags": "\\Seen",
            "flags_seen_at": "2026-07-30T00:00:00Z",
        }
    )
    repository.add_message(
        {
            **_message(folder_id, 11, uid=2),
            "internal_date": "2026-08-01T00:00:00Z",
            "imap_flags": "\\Flagged",
        }
    )
    repository.commit_batch()

    items = repository.list_flag_refresh_items("account", folder_id, 11, "2026-07-15T00:00:00Z")
    assert items == [{"uid": 2, "imap_flags": "\\Flagged", "flags_seen_at": None}]

    repository.begin_batch()
    repository.update_flags("account", folder_id, 11, 2, "\\Seen \\Flagged", "2026-08-18T00:00:00Z")
    repository.touch_flags_seen_at("account", folder_id, 11, [1], "2026-08-18T00:00:00Z")
    repository.commit_batch()

    rows = db_conn.execute(
        "SELECT uid, imap_flags, flags_seen_at FROM messages ORDER BY uid"
    ).fetchall()
    assert rows == [
        (1, "\\Seen", "2026-08-18T00:00:00Z"),
        (2, "\\Seen \\Flagged", "2026-08-18T00:00:00Z"),
    ]

    repository.set_highest_modseq(folder_id, 42)
    assert repository.list_folders("account")[0]["highest_modseq"] == 42
    repository.initialize_sync_cursors(folder_id, 12, 2)
    assert repository.list_folders("account")[0]["highest_modseq"] is None


def test_flag_refresh_index_exists(db_conn: sqlite3.Connection, tmp_path: Path) -> None:
    _repository(db_conn, tmp_path / "metadata.db")
    index_columns = db_conn.execute("PRAGMA index_info(idx_msg_flag_refresh)").fetchall()
    assert [row[2] for row in index_columns] == ["folder_id", "uidvalidity", "internal_date"]


def test_begin_batch_uses_immediate_transaction(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    repository, _ = _repository(db_conn, tmp_path / "metadata.db")
    statements: list[str] = []
    db_conn.set_trace_callback(statements.append)

    try:
        repository.begin_batch()
        repository.commit_batch()
    finally:
        db_conn.set_trace_callback(None)

    assert "BEGIN IMMEDIATE" in statements


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


def test_phase_four_a5_state_audit_and_reconstruction_operations(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    repository, folder_id = _repository(db_conn, tmp_path / "metadata.db")
    first_id = int(repository.add_message(_message(folder_id, 11)))
    second_id = int(
        repository.add_message(
            {
                **_message(folder_id, 11, uid=8),
                "relative_path": "eml/shared.eml",
                "local_state": "trashed",
                "trashed_at": "2026-07-01T00:00:00Z",
            },
            {"subject": "searchable", "body_text": "body"},
        )
    )
    shared_id = int(
        repository.add_message(
            {
                **_message(folder_id, 11, uid=9),
                "relative_path": "eml/shared.eml",
                "local_state": "active",
            }
        )
    )
    repository.record_audit(
        {
            "occurred_at": "2026-07-02T00:00:00Z",
            "operation": "local_trash",
            "account_id": "account",
            "message_id": "message-8",
            "subject": "Subject 11",
            "size_bytes": 10,
            "detail": "trashed",
        }
    )
    repository.record_audit({"occurred_at": "2026-07-03T00:00:00Z", "operation": "local_purge"})

    assert repository.get_message(first_id)["uid"] == 7  # type: ignore[index]
    assert [item["uid"] for item in repository.list_stored_messages()] == [7, 8, 9]
    assert repository.has_message_contents(second_id) is True
    assert repository.has_message_contents(first_id) is False
    assert [item["operation"] for item in repository.list_audit_log(10, 0)] == [
        "local_purge",
        "local_trash",
    ]
    assert [item["uid"] for item in repository.list_trashed(older_than="2026-08-01")] == [8]
    assert repository.count_path_references("account", "eml/shared.eml", second_id) == 1

    repository.set_local_state(second_id, "active")
    assert repository.list_trashed() == []
    repository.delete_message_contents(second_id)
    assert repository.has_message_contents(second_id) is False
    assert db_conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone() == (0,)
    repository.update_message_storage(first_id, None, None)
    assert repository.get_message(first_id)["relative_path"] is None  # type: ignore[index]
    repository.set_app_state("clean_shutdown", "0")
    assert repository.get_app_state("clean_shutdown") == "0"
    assert repository.get_app_state("missing") is None

    repository.set_local_state(second_id, "purged")
    assert repository.count_path_references("account", "eml/shared.eml", shared_id) == 0
