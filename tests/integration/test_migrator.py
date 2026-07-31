from __future__ import annotations

import re
import sqlite3
from importlib import resources
from pathlib import Path
from typing import cast

import pytest

import mail_dock.infrastructure.database.migrator as migrator
from mail_dock.domain.errors import MigrationError, SchemaVersionTooNewError
from mail_dock.infrastructure.database.connection import connect
from mail_dock.infrastructure.database.migrator import current_version, migrate

_UTC_ISO_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def test_empty_database_migrates_to_latest_version(
    db_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "metadata.db"

    assert migrate(db_conn, db_path) == 3
    assert current_version(db_conn) == 3
    assert db_conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_nonempty_v0_database_is_backed_up_before_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "metadata.db"
    connection = connect(db_path)
    try:
        connection.execute("CREATE TABLE legacy (value TEXT)")
        connection.execute("INSERT INTO legacy VALUES ('old')")
        connection.commit()
        assert migrate(connection, db_path) == 3
    finally:
        connection.close()

    backup_path = tmp_path / "metadata.db.bak.0"
    assert backup_path.is_file()
    backup = connect(backup_path, readonly=True)
    try:
        assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert backup.execute("SELECT value FROM legacy").fetchone() == ("old",)
    finally:
        backup.close()

    rerun = connect(db_path)
    try:
        assert migrate(rerun, db_path) == 3
    finally:
        rerun.close()
    assert not (tmp_path / "metadata.db.bak.0.1").exists()


def test_timestamp_migration_normalizes_legacy_values_and_defaults(
    db_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    initial_schema = resources.files("mail_dock").joinpath("migrations/001_init.sql")
    cursor_schema = resources.files("mail_dock").joinpath("migrations/002_sync_cursor.sql")
    db_conn.executescript(initial_schema.read_text(encoding="utf-8"))
    db_conn.executescript(cursor_schema.read_text(encoding="utf-8"))
    db_conn.execute("PRAGMA user_version = 2")
    db_conn.execute(
        "INSERT INTO accounts (id, provider_type, created_at) VALUES (?, ?, ?)",
        ("legacy", "imap", "2026-07-31 12:00:00"),
    )
    db_conn.execute(
        """
        INSERT INTO folders (account_id, raw_name, display_name)
        VALUES (?, ?, ?)
        """,
        ("legacy", "INBOX", "Inbox"),
    )
    folder_id = db_conn.execute("SELECT id FROM folders").fetchone()[0]
    db_conn.execute(
        """
        INSERT INTO messages (
            account_id, folder_id, content_key, source_item_key, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        ("legacy", folder_id, "key", "source", "2026-07-31 12:01:00"),
    )
    db_conn.execute(
        """
        INSERT INTO sync_failures (
            account_id, folder_id, uidvalidity, uid, error_class,
            first_failed_at, last_failed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "legacy",
            folder_id,
            1,
            7,
            "transient",
            "2026-07-31 12:02:00",
            "2026-07-31 12:03:00",
        ),
    )
    db_conn.execute(
        "INSERT INTO audit_log (occurred_at, operation) VALUES (?, ?)",
        ("2026-07-31 12:04:00", "test"),
    )
    db_conn.commit()

    assert migrate(db_conn, tmp_path / "metadata.db") == 3

    values = db_conn.execute(
        """
        SELECT created_at FROM accounts
        UNION ALL SELECT created_at FROM messages
        UNION ALL SELECT first_failed_at FROM sync_failures
        UNION ALL SELECT last_failed_at FROM sync_failures
        UNION ALL SELECT occurred_at FROM audit_log
        """
    ).fetchall()
    assert all(_UTC_ISO_TIMESTAMP.fullmatch(value[0]) for value in values)

    db_conn.execute("INSERT INTO accounts (id, provider_type) VALUES (?, ?)", ("new", "imap"))
    db_conn.execute(
        "INSERT INTO sync_failures (account_id, folder_id, uidvalidity, uid, error_class) "
        "VALUES (?, ?, ?, ?, ?)",
        ("legacy", folder_id, 1, 8, "transient"),
    )
    db_conn.execute("INSERT INTO audit_log (operation) VALUES (?)", ("test",))
    db_conn.commit()

    new_values = db_conn.execute(
        """
        SELECT created_at FROM accounts WHERE id = 'new'
        UNION ALL SELECT first_failed_at FROM sync_failures WHERE uid = 8
        UNION ALL SELECT last_failed_at FROM sync_failures WHERE uid = 8
        UNION ALL SELECT occurred_at FROM audit_log
        WHERE id = (SELECT MAX(id) FROM audit_log)
        """
    ).fetchall()
    assert all(_UTC_ISO_TIMESTAMP.fullmatch(value[0]) for value in new_values)


def test_database_newer_than_application_is_rejected(
    db_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    db_conn.execute("PRAGMA user_version = 999")
    db_conn.commit()

    with pytest.raises(SchemaVersionTooNewError):
        migrate(db_conn, tmp_path / "metadata.db")


def test_failed_migration_restores_foreign_keys_and_schema(
    db_conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    broken_path = tmp_path / "002_broken.sql"
    broken_path.write_text("CREATE TABLE broken (", encoding="utf-8")
    migration_path = cast(resources.abc.Traversable, broken_path)

    def fake_migration_files() -> list[tuple[int, resources.abc.Traversable]]:
        return [(2, migration_path)]

    monkeypatch.setattr(migrator, "_migration_files", fake_migration_files)

    with pytest.raises(MigrationError):
        migrate(db_conn, tmp_path / "metadata.db")

    assert current_version(db_conn) == 0
    assert db_conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
    assert db_conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'broken'").fetchone() is None
