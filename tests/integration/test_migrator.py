from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path
from typing import cast

import pytest

import mail_dock.infrastructure.database.migrator as migrator
from mail_dock.domain.errors import MigrationError, SchemaVersionTooNewError
from mail_dock.infrastructure.database.connection import connect
from mail_dock.infrastructure.database.migrator import current_version, migrate


def test_empty_database_migrates_to_latest_version(
    db_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "metadata.db"

    assert migrate(db_conn, db_path) == 2
    assert current_version(db_conn) == 2
    assert db_conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_nonempty_v0_database_is_backed_up_before_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "metadata.db"
    connection = connect(db_path)
    try:
        connection.execute("CREATE TABLE legacy (value TEXT)")
        connection.execute("INSERT INTO legacy VALUES ('old')")
        connection.commit()
        assert migrate(connection, db_path) == 2
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
        assert migrate(rerun, db_path) == 2
    finally:
        rerun.close()
    assert not (tmp_path / "metadata.db.bak.0.1").exists()


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
