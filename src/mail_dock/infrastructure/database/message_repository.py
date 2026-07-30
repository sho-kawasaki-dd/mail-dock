"""SQLite implementation of the small message repository port.

The repository owns database transactions but not the surrounding sync
workflow. Callers add records between ``begin_batch`` and ``commit_batch``;
there is deliberately no per-message commit path.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, cast

from mail_dock.domain.errors import DatabaseError
from mail_dock.domain.messages import StoredEml
from mail_dock.domain.repository import BaseMessageRepository, MessageContents, MessageRecord
from mail_dock.infrastructure.database.connection import ConnectionManager, checkpoint_truncate
from mail_dock.infrastructure.parsing.normalize import normalize_for_search
from mail_dock.infrastructure.storage.detach import classify_sqlite_error, storage_io

_ACCOUNT_COLUMNS = (
    "id",
    "provider_type",
    "display_name",
    "host",
    "port",
    "username",
    "is_enabled",
)
_FOLDER_COLUMNS = (
    "id",
    "account_id",
    "raw_name",
    "display_name",
    "uidvalidity",
    "last_seen_uid",
    "is_sync_target",
    "last_synced_at",
    "backfill_next_uid",
    "initial_sync_completed",
)
_MESSAGE_COLUMNS = (
    "message_id",
    "content_key",
    "source_item_key",
    "uid",
    "uidvalidity",
    "remote_state",
    "moved_to_folder_id",
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
    "imap_flags",
    "flags_seen_at",
    "in_reply_to",
    "references_ids",
    "thread_key",
    "last_seen_at",
)


class SqliteMessageRepository(BaseMessageRepository):
    """Persist message metadata using one connection owned by the caller thread."""

    def __init__(self, connection: sqlite3.Connection | ConnectionManager) -> None:
        self._connection = connection if isinstance(connection, sqlite3.Connection) else None
        self._connection_manager = connection if isinstance(connection, ConnectionManager) else None
        if self._connection is not None:
            self._connection.isolation_level = None

    def _conn(self) -> sqlite3.Connection:
        if self._connection is not None:
            return self._connection
        if self._connection_manager is None:
            raise DatabaseError("SQLite connection is not configured")
        connection = self._connection_manager.get_connection()
        connection.isolation_level = None
        return connection

    @contextmanager
    def _db_io(self, operation: str) -> Iterator[None]:
        try:
            with storage_io():
                yield
        except sqlite3.Error as error:
            classified = classify_sqlite_error(error)
            if classified is not error:
                raise classified from error
            raise DatabaseError(f"SQLite operation failed: {operation}") from error

    def _columns(self, table: str) -> set[str]:
        rows = self._conn().execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row[1]) for row in rows}

    @staticmethod
    def _row(cursor: sqlite3.Cursor, row: tuple[Any, ...]) -> MessageRecord:
        description = cursor.description
        if description is None:
            return {}
        return {str(column[0]): value for column, value in zip(description, row, strict=True)}

    def _rows(self, cursor: sqlite3.Cursor) -> list[MessageRecord]:
        return [self._row(cursor, cast(tuple[Any, ...], row)) for row in cursor.fetchall()]

    @staticmethod
    def _normalized_contents(contents: MessageContents) -> dict[str, str | None]:
        normalized: dict[str, str | None] = {}
        for key in ("subject_norm", "sender_norm", "body_text", "attachment_names"):
            value = contents.get(key)
            if value is None and key == "subject_norm":
                value = contents.get("subject")
            elif value is None and key == "sender_norm":
                value = contents.get("sender")
            normalized[key] = normalize_for_search(value) if value is not None else None
        return normalized

    def upsert_account(self, account: MessageRecord) -> str:
        account_id = str(account.get("id", account.get("account_id", "")))
        if not account_id:
            raise DatabaseError("Account id is required")
        values = {
            "id": account_id,
            "provider_type": str(account.get("provider_type", "onamae_imap")),
            "display_name": account.get("display_name"),
            "host": account.get("host"),
            "port": account.get("port", 993),
            "username": account.get("username"),
            "is_enabled": int(account.get("is_enabled", 1)),
        }
        with self._db_io("upsert account"):
            columns = ", ".join(_ACCOUNT_COLUMNS)
            placeholders = ", ".join("?" for _ in _ACCOUNT_COLUMNS)
            updates = ", ".join(f"{column}=excluded.{column}" for column in _ACCOUNT_COLUMNS[1:])
            self._conn().execute(
                f"INSERT INTO accounts ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}",
                tuple(values[column] for column in _ACCOUNT_COLUMNS),
            )
        return account_id

    def list_accounts(self) -> Sequence[MessageRecord]:
        with self._db_io("list accounts"):
            return self._rows(self._conn().execute("SELECT * FROM accounts ORDER BY id"))

    def upsert_folder(self, folder: MessageRecord) -> int:
        account_id = str(folder["account_id"])
        raw_name = str(folder["raw_name"])
        display_name = str(folder.get("display_name", raw_name))
        columns = self._columns("folders")
        insert_columns = [
            column for column in _FOLDER_COLUMNS if column in columns and column != "id"
        ]
        values: dict[str, Any] = {
            "account_id": account_id,
            "raw_name": raw_name,
            "display_name": display_name,
            "uidvalidity": folder.get("uidvalidity"),
            "last_seen_uid": folder.get("last_seen_uid", 0),
            "is_sync_target": int(folder.get("is_sync_target", 0)),
            "last_synced_at": folder.get("last_synced_at"),
            "backfill_next_uid": folder.get("backfill_next_uid"),
            "initial_sync_completed": int(folder.get("initial_sync_completed", 0)),
        }
        update_columns = [
            column
            for column in ("display_name", "uidvalidity", "last_synced_at")
            if column in columns and column in folder
        ]
        with self._db_io("upsert folder"):
            insert_sql = (
                f"INSERT INTO folders ({', '.join(insert_columns)}) VALUES "
                f"({', '.join('?' for _ in insert_columns)}) "
            )
            if update_columns:
                insert_sql += (
                    "ON CONFLICT(account_id, raw_name) DO UPDATE SET "
                    + ", ".join(f"{column}=excluded.{column}" for column in update_columns)
                )
            else:
                insert_sql += "ON CONFLICT(account_id, raw_name) DO NOTHING"
            self._conn().execute(insert_sql, tuple(values[column] for column in insert_columns))
            row = self._conn().execute(
                "SELECT id FROM folders WHERE account_id = ? AND raw_name = ?",
                (account_id, raw_name),
            ).fetchone()
        if row is None:
            raise DatabaseError("Folder upsert did not return an id")
        return int(row[0])

    def _folder_query(self, where: str, parameters: tuple[Any, ...]) -> Sequence[MessageRecord]:
        columns = [column for column in _FOLDER_COLUMNS if column in self._columns("folders")]
        cursor = self._conn().execute(
            f"SELECT {', '.join(columns)} FROM folders WHERE {where} ORDER BY id", parameters
        )
        return self._rows(cursor)

    def list_folders(self, account_id: str) -> Sequence[MessageRecord]:
        with self._db_io("list folders"):
            return self._folder_query("account_id = ?", (account_id,))

    def list_sync_targets(self, account_id: str) -> Sequence[MessageRecord]:
        with self._db_io("list sync targets"):
            return self._folder_query("account_id = ? AND is_sync_target = 1", (account_id,))

    def set_sync_target(self, account_id: str, raw_name: str, enabled: bool) -> None:
        with self._db_io("set sync target"):
            cursor = self._conn().execute(
                "UPDATE folders SET is_sync_target = ? WHERE account_id = ? AND raw_name = ?",
                (int(enabled), account_id, raw_name),
            )
            if cursor.rowcount == 0:
                raise DatabaseError(f"Folder does not exist: {raw_name}")

    def initialize_sync_cursors(self, folder_id: Any, uidvalidity: int, max_uid: int) -> None:
        columns = self._columns("folders")
        updates: dict[str, Any] = {"uidvalidity": uidvalidity, "last_seen_uid": max_uid}
        if "backfill_next_uid" in columns:
            updates["backfill_next_uid"] = max_uid
        if "initial_sync_completed" in columns:
            updates["initial_sync_completed"] = int(max_uid == 0)
        self._update_folder_columns(folder_id, updates)

    def update_sync_cursors(
        self,
        folder_id: Any,
        *,
        last_seen_uid: int | None = None,
        backfill_next_uid: int | None = None,
        initial_sync_completed: bool | None = None,
    ) -> None:
        updates: dict[str, Any] = {}
        columns = self._columns("folders")
        if last_seen_uid is not None:
            updates["last_seen_uid"] = last_seen_uid
        if backfill_next_uid is not None and "backfill_next_uid" in columns:
            updates["backfill_next_uid"] = backfill_next_uid
        if initial_sync_completed is not None and "initial_sync_completed" in columns:
            updates["initial_sync_completed"] = int(initial_sync_completed)
        if updates:
            self._update_folder_columns(folder_id, updates)

    def _update_folder_columns(self, folder_id: Any, updates: Mapping[str, Any]) -> None:
        with self._db_io("update sync cursors"):
            assignments = ", ".join(f"{column} = ?" for column in updates)
            self._conn().execute(
                f"UPDATE folders SET {assignments} WHERE id = ?",
                (*updates.values(), folder_id),
            )

    def add_message(self, record: MessageRecord, contents: MessageContents | None = None) -> Any:
        account_id = record["account_id"]
        folder_id = record["folder_id"]
        uid = record.get("uid")
        uidvalidity = record.get("uidvalidity")
        source_item_key = record["source_item_key"]
        with self._db_io("add message"):
            connection = self._conn()
            message_columns = [column for column in _MESSAGE_COLUMNS if column in record]
            insert_columns = ["account_id", "folder_id", *message_columns]
            conflict_target = (
                "(account_id, folder_id, uidvalidity, uid) WHERE uid IS NOT NULL"
                if uid is not None
                else "(account_id, folder_id, source_item_key) WHERE uid IS NULL"
            )
            update_columns = ", ".join(
                f"{column} = excluded.{column}" for column in message_columns
            )
            connection.execute(
                f"INSERT INTO messages ({', '.join(insert_columns)}) VALUES "
                f"({', '.join('?' for _ in insert_columns)}) "
                f"ON CONFLICT {conflict_target} DO UPDATE SET {update_columns}",
                tuple(record[column] for column in insert_columns),
            )
            identity: tuple[str, tuple[Any, ...]]
            if uid is None:
                identity = (
                    "account_id = ? AND folder_id = ? AND uid IS NULL "
                    "AND source_item_key = ?",
                    (account_id, folder_id, source_item_key),
                )
            else:
                identity = (
                    "account_id = ? AND folder_id = ? AND uidvalidity IS ? AND uid = ?",
                    (account_id, folder_id, uidvalidity, uid),
                )
            row = connection.execute(
                f"SELECT id FROM messages WHERE {identity[0]}", identity[1]
            ).fetchone()
            if row is None:
                raise DatabaseError("Message upsert did not return an id")
            message_id = int(row[0])

            if contents is not None:
                normalized = self._normalized_contents(contents)
                connection.execute(
                    "DELETE FROM message_contents WHERE message_id = ?", (message_id,)
                )
                connection.execute(
                    "INSERT INTO message_contents "
                    "(message_id, subject_norm, sender_norm, body_text, attachment_names) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        message_id,
                        normalized["subject_norm"],
                        normalized["sender_norm"],
                        normalized["body_text"],
                        normalized["attachment_names"],
                    ),
                )
        if message_id is None:
            raise DatabaseError("Message insert did not return an id")
        return int(message_id)

    def exists_source_item_key(self, account_id: str, folder_id: Any, source_item_key: str) -> bool:
        with self._db_io("check source item key"):
            row = self._conn().execute(
                "SELECT EXISTS(SELECT 1 FROM messages WHERE account_id = ? "
                "AND folder_id = ? AND source_item_key = ?)",
                (account_id, folder_id, source_item_key),
            ).fetchone()
        return bool(row and row[0])

    def find_stored_eml(self, account_id: str, file_hash: str) -> StoredEml | None:
        with self._db_io("find stored EML"):
            row = self._conn().execute(
                "SELECT relative_path, file_hash, size_bytes FROM messages "
                "WHERE account_id = ? AND file_hash = ? AND relative_path IS NOT NULL "
                "ORDER BY id LIMIT 1",
                (account_id, file_hash),
            ).fetchone()
        if row is None:
            return None
        return StoredEml(
            relative_path=str(row[0]),
            file_hash=str(row[1]),
            size_bytes=int(row[2] or 0),
            deduplicated=False,
        )

    def local_uids(self, account_id: str, folder_id: Any, uidvalidity: int) -> set[int]:
        with self._db_io("list local UIDs"):
            rows = self._conn().execute(
                "SELECT uid FROM messages WHERE account_id = ? AND folder_id = ? "
                "AND uidvalidity = ? AND uid IS NOT NULL",
                (account_id, folder_id, uidvalidity),
            ).fetchall()
        return {int(row[0]) for row in rows}

    def get_message_by_uid(
        self, account_id: str, folder_id: Any, uidvalidity: int, uid: int
    ) -> MessageRecord | None:
        """Return one concrete message row for remote-state reconciliation."""

        with self._db_io("get message by UID"):
            cursor = self._conn().execute(
                "SELECT * FROM messages WHERE account_id = ? AND folder_id = ? "
                "AND uidvalidity = ? AND uid = ?",
                (account_id, folder_id, uidvalidity, uid),
            )
            row = cursor.fetchone()
            return None if row is None else self._row(cursor, cast(tuple[Any, ...], row))

    def find_move_candidates(
        self,
        account_id: str,
        content_key: str,
        file_hash: str | None,
        exclude_folder_id: Any,
    ) -> Sequence[MessageRecord]:
        query = (
            "SELECT * FROM messages WHERE account_id = ? AND folder_id != ? "
            "AND content_key = ? AND remote_state = 'present'"
        )
        parameters: list[Any] = [account_id, exclude_folder_id, content_key]
        if file_hash is not None:
            query += " AND file_hash = ?"
            parameters.append(file_hash)
        query += " ORDER BY id"
        with self._db_io("find move candidates"):
            return self._rows(self._conn().execute(query, tuple(parameters)))

    def update_remote_state(
        self, message_id: Any, state: str, moved_to_folder_id: Any = None
    ) -> None:
        with self._db_io("update remote state"):
            self._conn().execute(
                "UPDATE messages SET remote_state = ?, moved_to_folder_id = ? WHERE id = ?",
                (state, moved_to_folder_id, message_id),
            )

    def record_failure(
        self,
        account_id: str,
        folder_id: Any,
        uidvalidity: int,
        uid: int,
        error_class: str,
        message: str,
    ) -> None:
        columns = self._columns("sync_failures")
        with self._db_io("record sync failure"):
            connection = self._conn()
            if "uidvalidity" in columns:
                connection.execute(
                    "INSERT INTO sync_failures "
                    "(account_id, folder_id, uidvalidity, uid, error_class, error_message) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(account_id, folder_id, uidvalidity, uid) DO UPDATE SET "
                    "error_class = excluded.error_class, error_message = excluded.error_message, "
                    "attempt_count = sync_failures.attempt_count + 1, "
                    "last_failed_at = CURRENT_TIMESTAMP",
                    (account_id, folder_id, uidvalidity, uid, error_class, message),
                )
            else:
                connection.execute(
                    "INSERT INTO sync_failures "
                    "(account_id, folder_id, uid, error_class, error_message) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(account_id, folder_id, uid) DO UPDATE SET "
                    "error_class = excluded.error_class, error_message = excluded.error_message, "
                    "attempt_count = sync_failures.attempt_count + 1, "
                    "last_failed_at = CURRENT_TIMESTAMP",
                    (account_id, folder_id, uid, error_class, message),
                )

    def pending_failures(
        self, account_id: str, folder_id: Any, uidvalidity: int
    ) -> Sequence[MessageRecord]:
        columns = self._columns("sync_failures")
        has_uidvalidity = "uidvalidity" in columns
        uidvalidity_filter = " AND uidvalidity = ?" if has_uidvalidity else ""
        parameters: tuple[Any, ...] = (
            (account_id, folder_id, uidvalidity, 10)
            if has_uidvalidity
            else (account_id, folder_id, 10)
        )
        with self._db_io("list pending failures"):
            return self._rows(
                self._conn().execute(
                    "SELECT * FROM sync_failures WHERE account_id = ? AND folder_id = ?"
                    f"{uidvalidity_filter} AND attempt_count < ? ORDER BY uid",
                    parameters,
                )
            )

    def clear_failure(self, account_id: str, folder_id: Any, uidvalidity: int, uid: int) -> None:
        columns = self._columns("sync_failures")
        has_uidvalidity = "uidvalidity" in columns
        uidvalidity_filter = " AND uidvalidity = ?" if has_uidvalidity else ""
        parameters: tuple[Any, ...] = (
            (account_id, folder_id, uidvalidity, uid)
            if has_uidvalidity
            else (account_id, folder_id, uid)
        )
        with self._db_io("clear sync failure"):
            self._conn().execute(
                "DELETE FROM sync_failures WHERE account_id = ? AND folder_id = ?"
                f"{uidvalidity_filter} AND uid = ?",
                parameters,
            )

    def list_reparse_targets(
        self, account_id: str | None, only_failed: bool
    ) -> Sequence[MessageRecord]:
        query = "SELECT m.* FROM messages m WHERE m.relative_path IS NOT NULL"
        parameters: list[Any] = []
        if account_id is not None:
            query += " AND m.account_id = ?"
            parameters.append(account_id)
        has_uidvalidity = "uidvalidity" in self._columns("sync_failures")
        if only_failed:
            query += (
                " AND EXISTS (SELECT 1 FROM sync_failures f WHERE f.account_id = m.account_id "
                "AND f.folder_id = m.folder_id AND f.uid = m.uid AND f.error_class = 'parse'"
            )
            if has_uidvalidity:
                query += " AND f.uidvalidity = m.uidvalidity"
            query += ")"
        query += " ORDER BY m.id"
        with self._db_io("list reparse targets"):
            return self._rows(self._conn().execute(query, tuple(parameters)))

    def update_message_contents(self, message_id: Any, contents: MessageContents) -> None:
        normalized = self._normalized_contents(contents)
        with self._db_io("update message contents"):
            connection = self._conn()
            connection.execute("DELETE FROM message_contents WHERE message_id = ?", (message_id,))
            connection.execute(
                "INSERT INTO message_contents "
                "(message_id, subject_norm, sender_norm, body_text, attachment_names) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    message_id,
                    normalized["subject_norm"],
                    normalized["sender_norm"],
                    normalized["body_text"],
                    normalized["attachment_names"],
                ),
            )

    def begin_batch(self) -> None:
        with self._db_io("begin batch"):
            connection = self._conn()
            if connection.in_transaction:
                raise DatabaseError("A database batch is already open")
            connection.execute("BEGIN IMMEDIATE")

    def commit_batch(self) -> None:
        with self._db_io("commit batch"):
            self._conn().commit()

    def checkpoint(self) -> None:
        with self._db_io("checkpoint database"):
            checkpoint_truncate(self._conn())