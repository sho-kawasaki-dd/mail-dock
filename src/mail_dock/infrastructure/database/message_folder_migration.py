"""Finalize the Phase 5.2b message/membership schema transition."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, cast

from mail_dock.domain.errors import MigrationError
from mail_dock.domain.message_identity import gmail_source_item_key, imap_source_item_key
from mail_dock.domain.ports import BaseManifestReader, BaseManifestWriter, JSONValue
from mail_dock.infrastructure.storage.detach import storage_io

_FOLDER_COLUMNS = (
    "folder_id",
    "uid",
    "uidvalidity",
    "remote_state",
    "moved_to_folder_id",
    "imap_flags",
    "flags_seen_at",
    "last_seen_at",
)
_MESSAGE_COLUMNS = (
    "id",
    "account_id",
    "message_id",
    "content_key",
    "source_item_key",
    "local_state",
    "trashed_at",
    "relative_path",
    "file_hash",
    "subject",
    "sender",
    "recipient",
    "cc",
    "date_sent",
    "internal_date",
    "size_bytes",
    "has_attachment",
    "in_reply_to",
    "references_ids",
    "thread_key",
    "created_at",
    "gmail_msgid",
    "gmail_thrid",
    "gmail_labels",
)
_MESSAGE_TABLE_SQL = """CREATE TABLE messages_new (
	id                  INTEGER PRIMARY KEY AUTOINCREMENT,
	account_id          TEXT NOT NULL REFERENCES accounts(id),
	message_id          TEXT,
	content_key         TEXT NOT NULL,
	source_item_key     TEXT NOT NULL,
	local_state         TEXT NOT NULL DEFAULT 'active',
	trashed_at          DATETIME,
	relative_path       TEXT,
	file_hash           TEXT,
	subject             TEXT,
	sender              TEXT,
	recipient           TEXT,
	cc                  TEXT,
	date_sent           DATETIME,
	internal_date       DATETIME,
	size_bytes          INTEGER,
	has_attachment      INTEGER NOT NULL DEFAULT 0,
	in_reply_to         TEXT,
	references_ids      TEXT,
	thread_key          TEXT,
	created_at          DATETIME DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
	gmail_msgid         TEXT,
	gmail_thrid         TEXT,
	gmail_labels        TEXT
)"""


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _dict_rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    if cursor.description is None:
        return []
    names = [str(column[0]) for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _has_legacy_columns(connection: sqlite3.Connection) -> bool:
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(messages)").fetchall()}
    return "folder_id" in columns


def _canonical_key(account_type: str, row: Mapping[str, Any]) -> str:
    if account_type == "pst_import":
        return str(row["source_item_key"])
    gmail_msgid = row.get("gmail_msgid")
    if isinstance(gmail_msgid, str) and gmail_msgid:
        try:
            return gmail_source_item_key(gmail_msgid)
        except ValueError as error:
            raise MigrationError("Gmail message has an invalid X-GM-MSGID") from error
    folder_raw_name = row.get("folder_raw_name")
    uidvalidity = row.get("uidvalidity")
    uid = row.get("uid")
    if (
        not isinstance(folder_raw_name, str)
        or not isinstance(uidvalidity, int)
        or not isinstance(uid, int)
    ):
        return str(row["source_item_key"])
    return imap_source_item_key(folder_raw_name, uidvalidity, uid)


def _membership_snapshot(
    account_id: str,
    source_item_key: str,
    memberships: list[dict[str, Any]],
) -> dict[str, JSONValue]:
    return cast(
        dict[str, JSONValue],
        {
            "event": "message_membership_snapshot",
            "account_id": account_id,
            "source_item_key": source_item_key,
            "memberships": memberships,
            "timestamp": _timestamp(),
        },
    )


def _account_rows(connection: sqlite3.Connection, account_id: str) -> list[dict[str, Any]]:
    return _dict_rows(
        connection.execute(
            "SELECT m.*, mf.folder_id, mf.uid, mf.uidvalidity, mf.remote_state, "
            "mf.moved_to_folder_id, mf.imap_flags, mf.flags_seen_at, mf.last_seen_at, "
            "f.raw_name AS folder_raw_name, moved.raw_name AS moved_to_folder_raw_name "
            "FROM messages AS m JOIN message_folders AS mf ON mf.message_id = m.id "
            "JOIN folders AS f ON f.id = mf.folder_id "
            "LEFT JOIN folders AS moved ON moved.id = mf.moved_to_folder_id "
            "WHERE m.account_id = ? ORDER BY m.id, mf.folder_id",
            (account_id,),
        )
    )


def _backfill_missing_memberships(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO message_folders "
        "(message_id, folder_id, uid, uidvalidity, remote_state, moved_to_folder_id, "
        "imap_flags, flags_seen_at, last_seen_at) "
        "SELECT m.id, m.folder_id, m.uid, m.uidvalidity, m.remote_state, "
        "m.moved_to_folder_id, m.imap_flags, m.flags_seen_at, m.last_seen_at "
        "FROM messages AS m WHERE NOT EXISTS (SELECT 1 FROM message_folders AS mf "
        "WHERE mf.message_id = m.id AND mf.folder_id = m.folder_id)"
    )


def _membership_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "folder_raw_name": row["folder_raw_name"],
        "uid": row["uid"],
        "uidvalidity": row["uidvalidity"],
        "remote_state": row["remote_state"],
        "moved_to_folder_raw_name": row["moved_to_folder_raw_name"],
        "imap_flags": row["imap_flags"],
        "flags_seen_at": row["flags_seen_at"],
        "last_seen_at": row["last_seen_at"],
    }


def _record_durable_events(
    rows_by_key: Mapping[str, list[dict[str, Any]]],
    account_id: str,
    writer: BaseManifestWriter,
    reader: BaseManifestReader,
) -> None:
    events = list(reader.read_all_events())
    latest_snapshots: dict[str, Mapping[str, JSONValue]] = {}
    existing_links: set[tuple[str, str]] = set()
    for event in events:
        if event.get("event") == "message_membership_snapshot":
            key = event.get("source_item_key")
            if isinstance(key, str):
                latest_snapshots[key] = event
        elif event.get("event") == "message_identity_linked":
            canonical = event.get("canonical_source_item_key")
            alias = event.get("alias_source_item_key")
            if isinstance(canonical, str) and isinstance(alias, str):
                existing_links.add((canonical, alias))

    for canonical_key, rows in rows_by_key.items():
        memberships_by_folder: dict[str, dict[str, Any]] = {}
        for row in rows:
            membership = _membership_dict(row)
            folder_name = str(membership["folder_raw_name"])
            current = memberships_by_folder.get(folder_name)
            if current is not None and current != membership:
                raise MigrationError(
                    "Cannot merge multiple remote memberships for one canonical message/folder"
                )
            memberships_by_folder[folder_name] = membership
        memberships = [memberships_by_folder[name] for name in sorted(memberships_by_folder)]
        previous = latest_snapshots.get(canonical_key)
        if previous is None or previous.get("memberships") != memberships:
            writer.append(_membership_snapshot(account_id, canonical_key, memberships))

        for row in rows:
            folder_raw_name = row.get("folder_raw_name")
            uidvalidity = row.get("uidvalidity")
            uid = row.get("uid")
            if (
                not isinstance(folder_raw_name, str)
                or not isinstance(uidvalidity, int)
                or not isinstance(uid, int)
            ):
                continue
            alias_key = imap_source_item_key(folder_raw_name, uidvalidity, uid)
            if alias_key == canonical_key:
                continue
            link_key = (canonical_key, alias_key)
            if link_key in existing_links:
                continue
            file_hash = row.get("file_hash")
            if not isinstance(file_hash, str) or not file_hash:
                raise MigrationError("Cannot durably link a message without its file hash")
            writer.append(
                cast(
                    dict[str, JSONValue],
                    {
                        "event": "message_identity_linked",
                        "account_id": account_id,
                        "canonical_source_item_key": canonical_key,
                        "alias_source_item_key": alias_key,
                        "evidence_kind": (
                            "gmail_msgid"
                            if canonical_key.startswith("gmail:")
                            else "imap_source_key_normalized"
                        ),
                        "file_hash": file_hash,
                        "timestamp": _timestamp(),
                    },
                )
            )
            existing_links.add(link_key)
    writer.flush_and_sync()


def _merge_account_rows(
    connection: sqlite3.Connection,
    account_id: str,
    rows_by_key: Mapping[str, list[dict[str, Any]]],
) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        for canonical_key, rows in rows_by_key.items():
            canonical = min(rows, key=lambda row: int(row["id"]))
            message_ids = [int(row["id"]) for row in rows]
            hashes = {row["file_hash"] for row in rows if row.get("file_hash") is not None}
            if len(hashes) > 1:
                raise MigrationError(
                    f"Cannot merge messages with different EML hashes: {canonical_key}"
                )
            stored_rows = [row for row in rows if row.get("relative_path") is not None]
            chosen_storage = stored_rows[0] if stored_rows else canonical
            local_state = (
                "active"
                if any(row.get("local_state") == "active" for row in rows)
                else "trashed"
                if any(row.get("local_state") == "trashed" for row in rows)
                else "purged"
            )
            connection.execute(
                "UPDATE messages SET source_item_key = ?, local_state = ?, "
                "trashed_at = ?, relative_path = ?, file_hash = ? WHERE id = ?",
                (
                    canonical_key,
                    local_state,
                    next((row.get("trashed_at") for row in rows if row.get("trashed_at")), None),
                    chosen_storage.get("relative_path"),
                    next(iter(hashes), None),
                    int(canonical["id"]),
                ),
            )
            for duplicate_id in message_ids:
                if duplicate_id == int(canonical["id"]):
                    continue
                content_exists = connection.execute(
                    "SELECT EXISTS(SELECT 1 FROM message_contents WHERE message_id = ?)",
                    (int(canonical["id"]),),
                ).fetchone()
                duplicate_content_exists = connection.execute(
                    "SELECT EXISTS(SELECT 1 FROM message_contents WHERE message_id = ?)",
                    (duplicate_id,),
                ).fetchone()
                if not bool(content_exists and content_exists[0]) and bool(
                    duplicate_content_exists and duplicate_content_exists[0]
                ):
                    connection.execute(
                        "UPDATE message_contents SET message_id = ? WHERE message_id = ?",
                        (int(canonical["id"]), duplicate_id),
                    )
                connection.execute(
                    "UPDATE pst_import_items SET message_row_id = ? WHERE message_row_id = ?",
                    (int(canonical["id"]), duplicate_id),
                )
                aliases = _dict_rows(
                    connection.execute(
                        "SELECT account_id, observed_source_item_key, evidence_kind "
                        "FROM message_identity_aliases WHERE message_id = ?",
                        (duplicate_id,),
                    )
                )
                for alias in aliases:
                    connection.execute(
                        "INSERT OR IGNORE INTO message_identity_aliases "
                        "(account_id, observed_source_item_key, message_id, evidence_kind) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            alias["account_id"],
                            alias["observed_source_item_key"],
                            int(canonical["id"]),
                            alias["evidence_kind"],
                        ),
                    )
                connection.execute(
                    "DELETE FROM message_identity_aliases WHERE message_id = ?", (duplicate_id,)
                )
                memberships = connection.execute(
                    "SELECT folder_id, uid, uidvalidity, remote_state, moved_to_folder_id, "
                    "imap_flags, flags_seen_at, last_seen_at FROM message_folders "
                    "WHERE message_id = ?",
                    (duplicate_id,),
                ).fetchall()
                for membership in memberships:
                    existing = connection.execute(
                        "SELECT uid, uidvalidity FROM message_folders "
                        "WHERE message_id = ? AND folder_id = ?",
                        (int(canonical["id"]), int(membership[0])),
                    ).fetchone()
                    if existing is not None and tuple(existing) != (membership[1], membership[2]):
                        raise MigrationError(
                            "Cannot merge duplicate Gmail memberships with different folder UIDs"
                        )
                    if existing is None:
                        connection.execute(
                            "UPDATE message_folders SET message_id = ? "
                            "WHERE message_id = ? AND folder_id = ?",
                            (int(canonical["id"]), duplicate_id, int(membership[0])),
                        )
                    else:
                        connection.execute(
                            "DELETE FROM message_folders WHERE message_id = ? AND folder_id = ?",
                            (duplicate_id, int(membership[0])),
                        )
                connection.execute("DELETE FROM messages WHERE id = ?", (duplicate_id,))

            for row in rows:
                if int(row["id"]) != int(canonical["id"]):
                    continue
                connection.execute(
                    "UPDATE message_folders SET remote_state = ?, moved_to_folder_id = ? "
                    "WHERE message_id = ? AND folder_id = ?",
                    (
                        row["remote_state"],
                        row["moved_to_folder_id"],
                        int(canonical["id"]),
                        int(row["folder_id"]),
                    ),
                )
            for row in rows:
                folder_raw_name = row.get("folder_raw_name")
                uidvalidity = row.get("uidvalidity")
                uid = row.get("uid")
                if (
                    isinstance(folder_raw_name, str)
                    and isinstance(uidvalidity, int)
                    and isinstance(uid, int)
                ):
                    alias_key = imap_source_item_key(folder_raw_name, uidvalidity, uid)
                else:
                    alias_key = str(row["source_item_key"])
                if alias_key != canonical_key and alias_key.startswith("imap:"):
                    connection.execute(
                        "INSERT OR IGNORE INTO message_identity_aliases "
                        "(account_id, observed_source_item_key, message_id, evidence_kind) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            account_id,
                            alias_key,
                            int(canonical["id"]),
                            "gmail_msgid" if canonical_key.startswith("gmail:") else "normalized",
                        ),
                    )
        connection.commit()
    except (MigrationError, sqlite3.Error):
        connection.rollback()
        raise


def _rebuild_messages_table(connection: sqlite3.Connection) -> None:
    connection.execute(_MESSAGE_TABLE_SQL)
    columns = ", ".join(_MESSAGE_COLUMNS)
    connection.execute(f"INSERT INTO messages_new ({columns}) SELECT {columns} FROM messages")
    connection.execute("DROP TABLE messages")
    connection.execute("ALTER TABLE messages_new RENAME TO messages")
    connection.execute(
        "CREATE UNIQUE INDEX uq_messages_source_item_key ON messages(account_id, source_item_key)"
    )
    connection.execute("CREATE INDEX idx_msg_key ON messages(account_id, content_key)")
    connection.execute("CREATE INDEX idx_msg_thread ON messages(thread_key, date_sent)")
    connection.execute("CREATE INDEX idx_msg_trash ON messages(local_state, trashed_at)")
    connection.execute(
        "CREATE INDEX idx_msg_purge ON messages(account_id, local_state, trashed_at)"
    )
    connection.execute("CREATE INDEX idx_msg_path ON messages(account_id, relative_path)")
    connection.execute(
        "CREATE INDEX idx_msg_gmsgid ON messages(account_id, gmail_msgid) "
        "WHERE gmail_msgid IS NOT NULL"
    )


def finalize_message_folders(
    connection: sqlite3.Connection,
    manifest_writer_factory: Callable[[str], BaseManifestWriter],
    manifest_reader_factory: Callable[[str], BaseManifestReader],
) -> bool:
    """Durably journal legacy memberships before canonicalizing the SQLite cache."""

    if not _has_legacy_columns(connection):
        return False
    try:
        with storage_io():
            _backfill_missing_memberships(connection)
            connection.commit()
        accounts = _dict_rows(
            connection.execute("SELECT id, provider_type FROM accounts ORDER BY id")
        )
        for account in accounts:
            account_id = str(account["id"])
            rows = _account_rows(connection, account_id)
            grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in rows:
                grouped[_canonical_key(str(account["provider_type"]), row)].append(row)
            for canonical_key, group in grouped.items():
                hashes = {row["file_hash"] for row in group if row.get("file_hash") is not None}
                if len(hashes) > 1:
                    raise MigrationError(f"EML hash mismatch while merging {canonical_key}")
            if account["provider_type"] != "pst_import":
                writer = manifest_writer_factory(account_id)
                try:
                    _record_durable_events(
                        grouped, account_id, writer, manifest_reader_factory(account_id)
                    )
                finally:
                    writer.close()
            _merge_account_rows(connection, account_id, grouped)

        if connection.in_transaction:
            raise MigrationError("Cannot rebuild messages while a transaction is active")
        with storage_io():
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("BEGIN IMMEDIATE")
            try:
                _rebuild_messages_table(connection)
                foreign_key_violations = connection.execute("PRAGMA foreign_key_check").fetchall()
                integrity = connection.execute("PRAGMA integrity_check").fetchone()
                if foreign_key_violations or integrity != ("ok",):
                    raise MigrationError("Message-folder finalization integrity check failed")
                connection.commit()
            except (MigrationError, sqlite3.Error):
                connection.rollback()
                raise
            finally:
                connection.execute("PRAGMA foreign_keys = ON")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise MigrationError("Message-folder finalization left foreign-key violations")
        return True
    except MigrationError:
        raise
    except sqlite3.Error as error:
        raise MigrationError("Could not finalize message-folder schema") from error
