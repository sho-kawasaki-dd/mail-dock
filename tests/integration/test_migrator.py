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
from mail_dock.infrastructure.database.message_folder_migration import finalize_message_folders
from mail_dock.infrastructure.database.message_repository import SqliteMessageRepository
from mail_dock.infrastructure.database.migrator import current_version, migrate
from mail_dock.infrastructure.storage.manifest import ManifestReader, ManifestWriter
from mail_dock.usecases.snapshots import reconcile_account_snapshots

_UTC_ISO_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def test_empty_database_migrates_to_latest_version(
    db_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "metadata.db"

    assert migrate(db_conn, db_path) == 9
    assert current_version(db_conn) == 9
    assert db_conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    account_columns = {row[1] for row in db_conn.execute("PRAGMA table_info(accounts)")}
    assert {"tls_mode", "ca_cert_path"}.issubset(account_columns)
    assert {
        "auth_type",
        "oauth_provider",
        "oauth_client_id",
        "oauth_tenant",
    }.issubset(account_columns)
    message_columns = {row[1] for row in db_conn.execute("PRAGMA table_info(messages)")}
    assert {"gmail_msgid", "gmail_thrid", "gmail_labels"}.issubset(message_columns)
    membership_columns = {row[1] for row in db_conn.execute("PRAGMA table_info(message_folders)")}
    assert membership_columns == {
        "message_id",
        "folder_id",
        "uid",
        "uidvalidity",
        "remote_state",
        "moved_to_folder_id",
        "imap_flags",
        "flags_seen_at",
        "last_seen_at",
    }
    db_conn.execute("INSERT INTO accounts (id, provider_type) VALUES (?, ?)", ("account", "imap"))
    assert db_conn.execute(
        "SELECT tls_mode, ca_cert_path, auth_type, oauth_provider, oauth_client_id, oauth_tenant "
        "FROM accounts WHERE id = ?",
        ("account",),
    ).fetchone() == ("implicit", None, "password", None, None, None)

    indexes = {
        row[1]: db_conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            (row[1],),
        ).fetchone()[0]
        for row in db_conn.execute("PRAGMA index_list(messages)")
    }
    indexes["idx_audit_recent"] = db_conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
        ("idx_audit_recent",),
    ).fetchone()[0]
    assert indexes["idx_audit_recent"] == (
        "CREATE INDEX idx_audit_recent\nON audit_log(occurred_at DESC)"
    )
    assert indexes["idx_msg_purge"] == (
        "CREATE INDEX idx_msg_purge\nON messages(account_id, local_state, trashed_at)"
    )
    assert indexes["idx_msg_path"] == (
        "CREATE INDEX idx_msg_path\nON messages(account_id, relative_path)"
    )
    assert indexes["idx_msg_gmsgid"] == (
        "CREATE INDEX idx_msg_gmsgid\n"
        "ON messages(account_id, gmail_msgid)\n"
        "WHERE gmail_msgid IS NOT NULL"
    )


def test_pst_import_migration_creates_import_tables_and_indexes(
    db_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    assert migrate(db_conn, tmp_path / "metadata.db") == 9

    import_columns = {row[1] for row in db_conn.execute("PRAGMA table_info(pst_imports)")}
    assert import_columns == {
        "id",
        "import_uuid",
        "account_id",
        "source_filename",
        "source_sha256",
        "source_size_bytes",
        "source_mtime",
        "readpst_version",
        "options_json",
        "status",
        "is_active",
        "replaces_id",
        "superseded_at",
        "total_files",
        "ingested_count",
        "failed_count",
        "staging_path",
        "started_at",
        "finished_at",
        "error_message",
    }
    item_columns = {row[1] for row in db_conn.execute("PRAGMA table_info(pst_import_items)")}
    assert item_columns == {
        "import_id",
        "source_item_key",
        "source_relative_path",
        "folder_relative_path",
        "source_size_bytes",
        "source_sha256",
        "final_relative_path",
        "message_row_id",
        "status",
        "error_class",
        "error_message",
        "attempt_count",
    }

    primary_key = [
        row[1]
        for row in sorted(
            db_conn.execute("PRAGMA table_info(pst_import_items)"),
            key=lambda column: column[5],
        )
        if row[5]
    ]
    assert primary_key == ["import_id", "source_item_key"]

    indexes = {
        row[1]: db_conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            (row[1],),
        ).fetchone()[0]
        for row in db_conn.execute("PRAGMA index_list(pst_imports)")
    }
    assert indexes["idx_pst_src"] == ("CREATE INDEX idx_pst_src\nON pst_imports(source_sha256)")
    assert indexes["uq_active_pst_source"] == (
        "CREATE UNIQUE INDEX uq_active_pst_source\n"
        "ON pst_imports(source_sha256)\n"
        "WHERE is_active = 1"
    )

    foreign_keys = {row[2] for row in db_conn.execute("PRAGMA foreign_key_list(pst_imports)")}
    assert foreign_keys == {"accounts", "pst_imports"}
    item_foreign_keys = {
        row[2] for row in db_conn.execute("PRAGMA foreign_key_list(pst_import_items)")
    }
    assert item_foreign_keys == {"pst_imports", "messages"}


def test_phase4_migration_backs_up_existing_v4_database(
    db_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    migration_dir = resources.files("mail_dock").joinpath("migrations")
    for migration_name in (
        "001_init.sql",
        "002_sync_cursor.sql",
        "003_timestamp_format.sql",
        "004_flag_refresh.sql",
    ):
        migration = migration_dir.joinpath(migration_name)
        db_conn.executescript(migration.read_text(encoding="utf-8"))
    db_conn.execute("PRAGMA user_version = 4")
    db_conn.execute(
        "INSERT INTO accounts (id, provider_type) VALUES (?, ?)",
        ("legacy", "imap"),
    )
    db_conn.commit()

    assert migrate(db_conn, tmp_path / "metadata.db") == 9

    backup_path = tmp_path / "metadata.db.bak.4"
    assert backup_path.is_file()
    backup = connect(backup_path, readonly=True)
    try:
        assert current_version(backup) == 4
        assert backup.execute("SELECT id FROM accounts").fetchone() == ("legacy",)
    finally:
        backup.close()


def test_nonempty_v0_database_is_backed_up_before_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "metadata.db"
    connection = connect(db_path)
    try:
        connection.execute("CREATE TABLE legacy (value TEXT)")
        connection.execute("INSERT INTO legacy VALUES ('old')")
        connection.commit()
        assert migrate(connection, db_path) == 9
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
        assert migrate(rerun, db_path) == 9
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

    assert migrate(db_conn, tmp_path / "metadata.db") == 9

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


def test_provider_type_normalization_records_manifest_before_db_update(
    db_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """The 007 migration only adds columns; ``reconcile_account_snapshots``

    (Group B) does the actual ``onamae_imap`` -> ``imap`` normalization as a
    separate, retryable post-migration step against real SQLite and a real
    on-disk manifest.
    """

    db_path = tmp_path / "metadata.db"
    assert migrate(db_conn, db_path) == 9
    db_conn.execute(
        "INSERT INTO accounts (id, provider_type, host, port, username) VALUES (?, ?, ?, ?, ?)",
        ("legacy-account", "onamae_imap", "imap.example.test", 993, "user"),
    )
    db_conn.commit()

    repository = SqliteMessageRepository(db_conn)

    def writer_factory(account_id: str) -> ManifestWriter:
        return ManifestWriter(tmp_path, account_id)

    def reader_factory(account_id: str) -> ManifestReader:
        return ManifestReader(tmp_path, account_id)

    normalized_count = reconcile_account_snapshots(repository, writer_factory, reader_factory)

    assert normalized_count == 1
    account = repository.list_accounts()[0]
    assert account["provider_type"] == "imap"
    assert account["tls_mode"] == "implicit"
    assert account["ca_cert_path"] is None

    reader = reader_factory("legacy-account")
    snapshot = next(
        event
        for event in reversed(list(reader.read_all_events()))
        if event["event"] == "account_snapshot"
    )
    assert snapshot["provider_type"] == "imap"
    assert snapshot["tls_mode"] == "implicit"

    # Retrying after the DB is already normalized must not error and must not
    # normalize a second time (idempotent post-migration step, D-4/D-34).
    assert reconcile_account_snapshots(repository, writer_factory, reader_factory) == 0


def test_message_folder_finalizer_journals_then_rebuilds_canonical_schema(
    db_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    assert migrate(db_conn, tmp_path / "metadata.db") == 9
    db_conn.execute("INSERT INTO accounts (id, provider_type) VALUES (?, ?)", ("account", "imap"))
    db_conn.execute(
        "INSERT INTO folders (account_id, raw_name, display_name) VALUES (?, ?, ?)",
        ("account", "INBOX", "Inbox"),
    )
    folder_id = int(db_conn.execute("SELECT id FROM folders").fetchone()[0])
    db_conn.execute(
        """INSERT INTO messages (
            account_id, folder_id, message_id, content_key, source_item_key, uid,
            uidvalidity, remote_state, local_state, relative_path, file_hash,
            subject, imap_flags, flags_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "account",
            folder_id,
            "<message@example.test>",
            "message-key",
            "42:7",
            7,
            42,
            "present",
            "active",
            "eml/account/message.eml",
            "a" * 64,
            "Subject",
            "\\Seen",
            "2026-09-26T00:00:00Z",
            "2026-09-26T00:00:00Z",
        ),
    )
    message_id = int(db_conn.execute("SELECT id FROM messages").fetchone()[0])
    db_conn.execute(
        "INSERT INTO message_folders "
        "(message_id, folder_id, uid, uidvalidity, remote_state, imap_flags, "
        "flags_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            message_id,
            folder_id,
            7,
            42,
            "present",
            "\\Seen",
            "2026-09-26T00:00:00Z",
            "2026-09-26T00:00:00Z",
        ),
    )
    db_conn.execute(
        "INSERT INTO message_contents (message_id, subject_norm) VALUES (?, ?)",
        (message_id, "subject"),
    )
    db_conn.commit()

    dependent_objects = {
        (row[0], row[1], row[2])
        for row in db_conn.execute(
            "SELECT type, name, tbl_name FROM sqlite_schema "
            "WHERE name IN ('mc_ai', 'mc_ad', 'mc_au')"
        )
    }
    assert dependent_objects == {
        ("trigger", "mc_ai", "message_contents"),
        ("trigger", "mc_ad", "message_contents"),
        ("trigger", "mc_au", "message_contents"),
    }
    assert {row[2] for row in db_conn.execute("PRAGMA foreign_key_list(message_contents)")} == {
        "messages"
    }
    assert {row[2] for row in db_conn.execute("PRAGMA foreign_key_list(pst_import_items)")} == {
        "pst_imports",
        "messages",
    }
    assert {row[2] for row in db_conn.execute("PRAGMA foreign_key_list(audit_log)")} == set()

    def writer_factory(account_id: str) -> ManifestWriter:
        return ManifestWriter(tmp_path, account_id)

    def reader_factory(account_id: str) -> ManifestReader:
        return ManifestReader(tmp_path, account_id)

    assert finalize_message_folders(db_conn, writer_factory, reader_factory)
    columns = {row[1] for row in db_conn.execute("PRAGMA table_info(messages)")}
    assert "folder_id" not in columns
    assert "uid" not in columns
    assert db_conn.execute(
        "SELECT source_item_key, subject FROM messages WHERE id = ?", (message_id,)
    ).fetchone() == ("imap:SU5CT1g:42:7", "Subject")
    assert db_conn.execute(
        "SELECT folder_id, uid, uidvalidity, imap_flags FROM message_folders WHERE message_id = ?",
        (message_id,),
    ).fetchone() == (folder_id, 7, 42, "\\Seen")
    assert db_conn.execute(
        "SELECT subject_norm FROM message_contents WHERE message_id = ?", (message_id,)
    ).fetchone() == ("subject",)
    assert db_conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert db_conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert {
        (row[0], row[1], row[2])
        for row in db_conn.execute(
            "SELECT type, name, tbl_name FROM sqlite_schema "
            "WHERE name IN ('mc_ai', 'mc_ad', 'mc_au')"
        )
    } == dependent_objects
    events = reader_factory("account").read_all_events()
    snapshot = next(
        event for event in events if event.get("event") == "message_membership_snapshot"
    )
    assert snapshot["source_item_key"] == "imap:SU5CT1g:42:7"
    assert finalize_message_folders(db_conn, writer_factory, reader_factory) is False


def test_message_folder_finalizer_merges_gmail_duplicates_and_preserves_memberships(
    db_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    assert migrate(db_conn, tmp_path / "metadata.db") == 9
    db_conn.execute(
        "INSERT INTO accounts (id, provider_type, oauth_provider) VALUES (?, ?, ?)",
        ("gmail", "imap", "google"),
    )
    folder_ids: list[int] = []
    for raw_name in ("INBOX", "[Gmail]/Important"):
        db_conn.execute(
            "INSERT INTO folders (account_id, raw_name, display_name) VALUES (?, ?, ?)",
            ("gmail", raw_name, raw_name),
        )
        folder_ids.append(int(db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]))
    message_ids: list[int] = []
    for folder_id, uid, uidvalidity, local_state in (
        (folder_ids[0], 7, 42, "trashed"),
        (folder_ids[1], 3, 81, "active"),
    ):
        db_conn.execute(
            """INSERT INTO messages (
                account_id, folder_id, content_key, source_item_key, uid, uidvalidity,
                local_state, relative_path, file_hash, gmail_msgid
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "gmail",
                folder_id,
                "gmail-content",
                f"{uidvalidity}:{uid}",
                uid,
                uidvalidity,
                local_state,
                "eml/gmail/message.eml",
                "b" * 64,
                "123456789",
            ),
        )
        message_id = int(db_conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        message_ids.append(message_id)
        db_conn.execute(
            "INSERT INTO message_folders "
            "(message_id, folder_id, uid, uidvalidity, remote_state) "
            "VALUES (?, ?, ?, ?, 'present')",
            (message_id, folder_id, uid, uidvalidity),
        )
        db_conn.execute(
            "INSERT INTO message_contents (message_id, subject_norm) VALUES (?, ?)",
            (message_id, f"subject-{uid}"),
        )
    db_conn.commit()

    def writer_factory(account_id: str) -> ManifestWriter:
        return ManifestWriter(tmp_path, account_id)

    def reader_factory(account_id: str) -> ManifestReader:
        return ManifestReader(tmp_path, account_id)

    assert finalize_message_folders(db_conn, writer_factory, reader_factory)
    canonical_id = min(message_ids)
    assert db_conn.execute(
        "SELECT id, source_item_key, local_state FROM messages WHERE account_id = ?",
        ("gmail",),
    ).fetchall() == [(canonical_id, "gmail:123456789", "active")]
    assert db_conn.execute(
        "SELECT folder_id, uid FROM message_folders WHERE message_id = ? ORDER BY folder_id",
        (canonical_id,),
    ).fetchall() == [(folder_ids[0], 7), (folder_ids[1], 3)]
    assert db_conn.execute(
        "SELECT COUNT(*) FROM message_identity_aliases WHERE message_id = ?", (canonical_id,)
    ).fetchone() == (2,)
    assert any(
        event.get("event") == "message_identity_linked"
        for event in reader_factory("gmail").read_all_events()
    )
