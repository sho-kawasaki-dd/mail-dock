from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

import pytest

from mail_dock.infrastructure.database.migrator import migrate


def test_sync_cursor_migration_preserves_v1_data_and_adds_generation_keys(
	db_conn: sqlite3.Connection,
	tmp_path: Path,
) -> None:
	initial_schema = resources.files("mail_dock").joinpath("migrations/001_init.sql")
	db_conn.executescript(initial_schema.read_text(encoding="utf-8"))
	db_conn.execute("PRAGMA user_version = 1")
	db_conn.execute(
		"INSERT INTO accounts (id, provider_type) VALUES (?, ?)",
		("account", "imap"),
	)
	db_conn.execute(
		"""
		INSERT INTO folders (
			account_id, raw_name, display_name, uidvalidity, last_seen_uid
		) VALUES (?, ?, ?, ?, ?)
		""",
		("account", "INBOX", "Inbox", 12, 42),
	)
	folder_id = db_conn.execute("SELECT id FROM folders").fetchone()[0]
	db_conn.execute(
		"""
		INSERT INTO sync_failures (
			account_id, folder_id, uid, error_class, error_message
		) VALUES (?, ?, ?, ?, ?)
		""",
		("account", folder_id, 7, "transient", "connection lost"),
	)
	db_conn.commit()

	assert migrate(db_conn, tmp_path / "metadata.db") == 2

	folder_columns = {
		row[1]: (row[2], row[3], row[4], row[5])
		for row in db_conn.execute("PRAGMA table_info(folders)")
	}
	assert folder_columns["backfill_next_uid"][0] == "INTEGER"
	assert folder_columns["initial_sync_completed"] == ("INTEGER", 1, "0", 0)

	folder = db_conn.execute(
		"SELECT last_seen_uid, backfill_next_uid, initial_sync_completed FROM folders"
	).fetchone()
	assert folder == (42, None, 0)

	failure = db_conn.execute(
		"""
		SELECT account_id, folder_id, uidvalidity, uid, error_class, error_message
		FROM sync_failures
		"""
	).fetchone()
	assert failure == ("account", folder_id, 0, 7, "transient", "connection lost")

	db_conn.execute(
		"""
		INSERT INTO sync_failures (
			account_id, folder_id, uidvalidity, uid, error_class
		) VALUES (?, ?, ?, ?, ?)
		""",
		("account", folder_id, 13, 7, "permanent"),
	)
	with pytest.raises(sqlite3.IntegrityError):
		db_conn.execute(
			"""
			INSERT INTO sync_failures (
				account_id, folder_id, uidvalidity, uid, error_class
			) VALUES (?, ?, ?, ?, ?)
			""",
			("account", folder_id, 13, 7, "permanent"),
		)

	index = db_conn.execute(
		"SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'idx_msg_file_hash'"
	).fetchone()
	assert index is not None
	assert "WHERE file_hash IS NOT NULL" in index[0]