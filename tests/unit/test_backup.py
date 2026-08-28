import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mail_dock import config
from mail_dock.__main__ import StorageSession
from mail_dock.infrastructure.database.backup import (
    backup_database,
    backup_is_due,
    local_backup_is_allowed,
)


def test_storage_session_creates_database_backup_on_normal_exit(
    tmp_storage_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "save", lambda value: None)
    monkeypatch.setattr("mail_dock.__main__.check_free_space", lambda path: None)

    with StorageSession(config.AppConfig(), tmp_storage_root):
        pass

    backup_path = tmp_storage_root / "metadata.db.bak"
    assert backup_path.is_file()
    database = sqlite3.connect(tmp_storage_root / "metadata.db")
    try:
        assert (
            database.execute(
                "SELECT value FROM app_state WHERE key = 'last_database_backup_at'"
            ).fetchone()
            is not None
        )
    finally:
        database.close()
    backup = sqlite3.connect(backup_path)
    try:
        assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        backup.close()


def test_storage_session_does_not_create_local_backup_by_default(
    tmp_storage_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "save", lambda value: None)
    monkeypatch.setattr(config, "config_dir", lambda: tmp_path / "config")
    monkeypatch.setattr("mail_dock.__main__.check_free_space", lambda path: None)

    with StorageSession(config.AppConfig(), tmp_storage_root):
        pass

    assert not (tmp_path / "config" / "metadata.db.bak").exists()


def test_backup_database_replaces_destination_and_passes_integrity_check(tmp_path: Path) -> None:
    source_path = tmp_path / "metadata.db"
    destination = tmp_path / "metadata.db.bak"
    source = sqlite3.connect(source_path)
    try:
        source.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, subject TEXT)")
        source.execute("INSERT INTO messages (subject) VALUES ('subject')")
        source.commit()
        backup_database(source, destination)
    finally:
        source.close()

    backup = sqlite3.connect(destination)
    try:
        assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert backup.execute("SELECT subject FROM messages").fetchone() == ("subject",)
    finally:
        backup.close()
    assert not list(tmp_path.glob(".metadata.db.bak.*.tmp"))


def test_backup_is_due_for_missing_invalid_and_expired_timestamps() -> None:
    now = datetime(2026, 8, 29, tzinfo=UTC)
    assert backup_is_due(None, now=now)
    assert backup_is_due("not-a-timestamp", now=now)
    assert backup_is_due((now - timedelta(days=7)).isoformat(), now=now)
    assert not backup_is_due((now - timedelta(days=6, hours=23)).isoformat(), now=now)


def test_local_backup_is_blocked_when_storage_encryption_is_unknown_to_destination() -> None:
    assert not local_backup_is_allowed("encrypted")
    assert local_backup_is_allowed("encrypted", "encrypted")
    assert not local_backup_is_allowed("encrypted", "unencrypted")
    assert local_backup_is_allowed("unencrypted")
    assert local_backup_is_allowed("unknown")


def test_backup_database_replaces_existing_destination(tmp_path: Path) -> None:
    destination = tmp_path / "metadata.db.bak"
    destination.write_text("previous backup", encoding="utf-8")
    source = sqlite3.connect(":memory:")
    try:
        source.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY)")
        source.commit()
        backup_database(source, destination)
    finally:
        source.close()

    assert destination.read_bytes() != b"previous backup"
