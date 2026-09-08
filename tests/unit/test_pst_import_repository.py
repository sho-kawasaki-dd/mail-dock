from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mail_dock.infrastructure.database.connection import connect
from mail_dock.infrastructure.database.migrator import migrate
from mail_dock.infrastructure.database.pst_import_repository import (
    SqlitePstImportRepository,
)


def _repository(
    connection: sqlite3.Connection, db_path: Path
) -> tuple[SqlitePstImportRepository, int]:
    migrate(connection, db_path)
    connection.execute(
        "INSERT INTO accounts (id, provider_type) VALUES (?, ?)",
        ("pst-account", "pst_import"),
    )
    cursor = connection.execute(
        "INSERT INTO folders (account_id, raw_name, display_name) VALUES (?, ?, ?)",
        ("pst-account", "Inbox", "Inbox"),
    )
    connection.commit()
    if cursor.lastrowid is None:
        raise AssertionError("Folder insert did not return an id")
    return SqlitePstImportRepository(connection), int(cursor.lastrowid)


def _import_record(
    uuid: str, *, active: int = 0, replaces_id: int | None = None
) -> dict[str, object]:
    return {
        "import_uuid": uuid,
        "account_id": "pst-account",
        "source_filename": "archive.pst",
        "source_sha256": "a" * 64,
        "status": "completed",
        "is_active": active,
        "replaces_id": replaces_id,
    }


def _item(import_id: int, key: str, message_id: int | None = None) -> dict[str, object]:
    return {
        "import_id": import_id,
        "source_item_key": key,
        "source_relative_path": f"Inbox/{key}.eml",
        "folder_relative_path": "Inbox",
        "source_size_bytes": 10,
        "source_sha256": "b" * 64,
        "status": "discovered",
        "message_row_id": message_id,
    }


def test_pst_import_items_are_idempotent_and_batches_are_atomic(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    repository, folder_id = _repository(db_conn, tmp_path / "metadata.db")
    import_id = repository.create_import(_import_record("first"))

    repository.upsert_import_item(_item(import_id, "item-1"))
    repository.upsert_import_item({**_item(import_id, "item-1"), "status": "saved"})
    assert [item["status"] for item in repository.list_items(import_id)] == ["saved"]
    assert repository.list_incomplete_items(import_id) == []

    repository.begin_batch()
    message_id = repository.add_message(
        {
            "account_id": "pst-account",
            "folder_id": folder_id,
            "content_key": "content-1",
            "source_item_key": "item-2",
            "remote_state": "no_remote",
            "relative_path": "eml/pst/item-2.eml",
            "file_hash": "c" * 64,
            "size_bytes": 10,
        }
    )
    repository.upsert_import_item(_item(import_id, "item-2", message_id))
    assert db_conn.execute("SELECT COUNT(*) FROM messages").fetchone() == (1,)
    other_connection = connect(tmp_path / "metadata.db")
    try:
        assert other_connection.execute("SELECT COUNT(*) FROM messages").fetchone() == (0,)
    finally:
        other_connection.close()
    repository.rollback_batch()
    assert db_conn.execute("SELECT COUNT(*) FROM messages").fetchone() == (0,)


def test_pst_message_rejects_remote_fields(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    repository, folder_id = _repository(db_conn, tmp_path / "metadata.db")
    record = {
        "account_id": "pst-account",
        "folder_id": folder_id,
        "content_key": "content-1",
        "source_item_key": "item-1",
        "remote_state": "present",
    }

    with pytest.raises(ValueError, match="no_remote"):
        repository.add_message(record)

    for field in (
        "uid",
        "uidvalidity",
        "imap_flags",
        "flags_seen_at",
        "last_seen_at",
        "internal_date",
    ):
        with pytest.raises(ValueError, match="NULL"):
            repository.add_message(
                {
                    **record,
                    "remote_state": "no_remote",
                    field: "remote-value",
                }
            )

    repository.add_message({**record, "remote_state": "no_remote"})
    assert db_conn.execute(
        "SELECT uid, uidvalidity, imap_flags, flags_seen_at, last_seen_at, "
        "internal_date FROM messages WHERE source_item_key = ?",
        ("item-1",),
    ).fetchone() == (None, None, None, None, None, None)


def test_generation_activation_and_restore_switch_message_visibility(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    repository, folder_id = _repository(db_conn, tmp_path / "metadata.db")
    old_id = repository.create_import(_import_record("old", active=1))
    new_id = repository.create_import(
        _import_record("new", active=0, replaces_id=old_id) | {"source_sha256": "d" * 64}
    )

    repository.begin_batch()
    old_message = repository.add_message(
        {
            "account_id": "pst-account",
            "folder_id": folder_id,
            "content_key": "old-content",
            "source_item_key": "old-item",
            "remote_state": "no_remote",
            "local_state": "active",
        }
    )
    new_message = repository.add_message(
        {
            "account_id": "pst-account",
            "folder_id": folder_id,
            "content_key": "new-content",
            "source_item_key": "new-item",
            "remote_state": "no_remote",
            "local_state": "active",
        }
    )
    repository.upsert_import_item(_item(old_id, "old-item", old_message) | {"status": "saved"})
    repository.upsert_import_item(_item(new_id, "new-item", new_message) | {"status": "saved"})
    repository.commit_batch()

    repository.activate_generation(new_id, old_id)
    assert db_conn.execute(
        "SELECT is_active, status FROM pst_imports WHERE id = ?", (new_id,)
    ).fetchone() == (1, "completed")
    assert db_conn.execute(
        "SELECT is_active, status FROM pst_imports WHERE id = ?", (old_id,)
    ).fetchone() == (0, "superseded")
    assert db_conn.execute(
        "SELECT local_state FROM messages WHERE id = ?", (old_message,)
    ).fetchone() == ("trashed",)

    repository.restore_generation(old_id)
    assert db_conn.execute(
        "SELECT is_active, status FROM pst_imports WHERE id = ?", (old_id,)
    ).fetchone() == (1, "completed")
    assert db_conn.execute(
        "SELECT local_state FROM messages WHERE id = ?", (old_message,)
    ).fetchone() == ("active",)
    assert db_conn.execute(
        "SELECT local_state FROM messages WHERE id = ?", (new_message,)
    ).fetchone() == ("trashed",)