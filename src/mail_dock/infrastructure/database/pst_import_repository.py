"""SQLite persistence for PST import jobs and staged items."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any, cast

from mail_dock.domain.errors import DatabaseError
from mail_dock.domain.repository import (
    BasePstImportRepository,
    MessageContents,
    MessageRecord,
)
from mail_dock.infrastructure.database.connection import ConnectionManager
from mail_dock.infrastructure.database.message_repository import SqliteMessageRepository
from mail_dock.infrastructure.storage.detach import classify_sqlite_error, storage_io

_UNSET = object()
_IMPORT_COLUMNS = (
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
)
_ITEM_COLUMNS = (
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
)
_PST_MESSAGE_NULL_FIELDS = (
    "uid",
    "uidvalidity",
    "imap_flags",
    "flags_seen_at",
    "last_seen_at",
    "internal_date",
)
_INCOMPLETE_IMPORT_STATUSES = (
    "extracting",
    "ready_to_ingest",
    "ingesting",
    "cancelled_resumable",
    "failed_resumable",
)


def validate_no_remote_message(record: MessageRecord) -> None:
    """Validate the fields that must never be populated for a PST message."""

    if record.get("remote_state") != "no_remote":
        raise ValueError("PST messages must use remote_state='no_remote'")
    invalid_fields = [field for field in _PST_MESSAGE_NULL_FIELDS if record.get(field) is not None]
    if invalid_fields:
        raise ValueError(f"PST messages must leave fields NULL: {', '.join(invalid_fields)}")


class SqlitePstImportRepository(BasePstImportRepository):
    """Persist PST metadata on the same thread-owned connection as messages."""

    def __init__(self, connection: sqlite3.Connection | ConnectionManager) -> None:
        self._connection = connection if isinstance(connection, sqlite3.Connection) else None
        self._connection_manager = connection if isinstance(connection, ConnectionManager) else None
        if self._connection is not None:
            self._connection.isolation_level = None
        self._message_repository = SqliteMessageRepository(self._conn())

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

    @staticmethod
    def _row(cursor: sqlite3.Cursor, row: tuple[Any, ...]) -> MessageRecord:
        if cursor.description is None:
            return {}
        return {
            str(column[0]): value
            for column, value in zip(cursor.description, row, strict=True)
        }

    def _rows(self, cursor: sqlite3.Cursor) -> list[MessageRecord]:
        return [self._row(cursor, cast(tuple[Any, ...], row)) for row in cursor.fetchall()]

    def upsert_folder(self, folder: MessageRecord) -> int:
        account_id = folder.get("account_id")
        raw_name = folder.get("raw_name")
        display_name = folder.get("display_name")
        if not all(
            isinstance(value, str) and value for value in (account_id, raw_name, display_name)
        ):
            raise ValueError("PST folder account_id, raw_name, and display_name are required")
        return self._message_repository.upsert_folder(
            {
                "account_id": account_id,
                "raw_name": raw_name,
                "display_name": display_name,
            }
        )

    def create_import(self, record: MessageRecord) -> int:
        required = ("import_uuid", "account_id", "source_filename", "source_sha256", "status")
        missing = [column for column in required if record.get(column) is None]
        if missing:
            raise ValueError(f"PST import fields are required: {', '.join(missing)}")
        columns = [column for column in _IMPORT_COLUMNS if column in record]
        values = tuple(record[column] for column in columns)
        with self._db_io("create PST import"):
            cursor = self._conn().execute(
                f"INSERT INTO pst_imports ({', '.join(columns)}) VALUES "
                f"({', '.join('?' for _ in columns)})",
                values,
            )
            if cursor.lastrowid is None:
                raise DatabaseError("PST import insert did not return an id")
            return int(cursor.lastrowid)

    def update_import_status(
        self,
        import_id: int,
        status: str,
        *,
        total_files: int | object | None = _UNSET,
        ingested_count: int | object | None = _UNSET,
        failed_count: int | object | None = _UNSET,
        staging_path: str | object | None = _UNSET,
        finished_at: str | object | None = _UNSET,
        error_message: str | object | None = _UNSET,
    ) -> None:
        values: dict[str, Any] = {"status": status}
        for name, value in (
            ("total_files", total_files),
            ("ingested_count", ingested_count),
            ("failed_count", failed_count),
            ("staging_path", staging_path),
            ("finished_at", finished_at),
            ("error_message", error_message),
        ):
            if value is not _UNSET:
                values[name] = value
        assignments = ", ".join(f"{column} = ?" for column in values)
        with self._db_io("update PST import status"):
            cursor = self._conn().execute(
                f"UPDATE pst_imports SET {assignments} WHERE id = ?",
                (*values.values(), import_id),
            )
            if cursor.rowcount != 1:
                raise DatabaseError(f"PST import does not exist: {import_id}")

    def find_active_by_source_sha256(self, source_sha256: str) -> MessageRecord | None:
        with self._db_io("find active PST import"):
            cursor = self._conn().execute(
                "SELECT * FROM pst_imports WHERE source_sha256 = ? AND is_active = 1",
                (source_sha256,),
            )
            row = cursor.fetchone()
            return None if row is None else self._row(cursor, cast(tuple[Any, ...], row))

    def find_incomplete_by_source_sha256(self, source_sha256: str) -> Sequence[MessageRecord]:
        placeholders = ", ".join("?" for _ in _INCOMPLETE_IMPORT_STATUSES)
        with self._db_io("find incomplete PST imports"):
            return self._rows(
                self._conn().execute(
                    "SELECT * FROM pst_imports WHERE source_sha256 = ? "
                    f"AND status IN ({placeholders}) ORDER BY id",
                    (source_sha256, *_INCOMPLETE_IMPORT_STATUSES),
                )
            )

    def upsert_import_item(self, record: MessageRecord) -> None:
        required = (
            "import_id",
            "source_item_key",
            "source_relative_path",
            "folder_relative_path",
            "status",
        )
        missing = [column for column in required if record.get(column) is None]
        if missing:
            raise ValueError(f"PST import item fields are required: {', '.join(missing)}")
        values = {column: record.get(column) for column in _ITEM_COLUMNS}
        values["attempt_count"] = record.get("attempt_count", 0)
        columns = ", ".join(_ITEM_COLUMNS)
        updates = ", ".join(
            f"{column} = excluded.{column}"
            for column in _ITEM_COLUMNS
            if column not in {"import_id", "source_item_key"}
        )
        with self._db_io("upsert PST import item"):
            self._conn().execute(
                f"INSERT INTO pst_import_items ({columns}) VALUES "
                f"({', '.join('?' for _ in _ITEM_COLUMNS)}) "
                f"ON CONFLICT(import_id, source_item_key) DO UPDATE SET {updates}",
                tuple(values[column] for column in _ITEM_COLUMNS),
            )

    def list_incomplete_items(self, import_id: int) -> Sequence[MessageRecord]:
        with self._db_io("list incomplete PST import items"):
            return self._rows(
                self._conn().execute(
                    "SELECT * FROM pst_import_items "
                    "WHERE import_id = ? AND status NOT IN ('saved', 'completed') "
                    "ORDER BY source_item_key",
                    (import_id,),
                )
            )

    def list_items(self, import_id: int) -> Sequence[MessageRecord]:
        with self._db_io("list PST import items"):
            return self._rows(
                self._conn().execute(
                    "SELECT * FROM pst_import_items WHERE import_id = ? "
                    "ORDER BY source_item_key",
                    (import_id,),
                )
            )

    def add_message(self, record: MessageRecord, contents: MessageContents | None = None) -> int:
        """Insert a PST message through the established message repository logic."""

        validate_no_remote_message(record)
        normalized = dict(record)
        for field in _PST_MESSAGE_NULL_FIELDS:
            normalized[field] = None
        return int(self._message_repository.add_message(normalized, contents))

    def begin_batch(self) -> None:
        with self._db_io("begin PST import batch"):
            connection = self._conn()
            if connection.in_transaction:
                raise DatabaseError("A database batch is already open")
            connection.execute("BEGIN IMMEDIATE")

    def commit_batch(self) -> None:
        with self._db_io("commit PST import batch"):
            self._conn().commit()

    def rollback_batch(self) -> None:
        with self._db_io("rollback PST import batch"):
            self._conn().rollback()

    def _run_generation_change(self, operation: str, callback: Any) -> None:
        connection = self._conn()
        owns_transaction = not connection.in_transaction
        try:
            if owns_transaction:
                connection.execute("BEGIN IMMEDIATE")
            callback(connection)
            if owns_transaction:
                connection.commit()
        except Exception:
            if owns_transaction:
                connection.rollback()
            raise DatabaseError(f"Could not {operation}") from None

    def activate_generation(self, import_id: int, replaces_id: int) -> None:
        def activate(connection: sqlite3.Connection) -> None:
            new_row = connection.execute(
                "SELECT status FROM pst_imports WHERE id = ?", (import_id,)
            ).fetchone()
            old_row = connection.execute(
                "SELECT is_active FROM pst_imports WHERE id = ?", (replaces_id,)
            ).fetchone()
            if new_row is None or old_row is None:
                raise ValueError("PST generation does not exist")
            if new_row[0] not in {"completed", "completed_with_errors"}:
                raise ValueError("Only a completed PST generation can become active")
            if not old_row[0]:
                raise ValueError("The replaced PST generation is not active")
            connection.execute(
                "UPDATE messages SET local_state = 'active', trashed_at = NULL "
                "WHERE id IN (SELECT message_row_id FROM pst_import_items WHERE import_id = ?)",
                (import_id,),
            )
            connection.execute(
                "UPDATE messages SET local_state = 'trashed', "
                "trashed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
                "WHERE id IN (SELECT message_row_id FROM pst_import_items WHERE import_id = ?)",
                (replaces_id,),
            )
            connection.execute(
                "UPDATE pst_imports SET is_active = 0, status = 'superseded', "
                "superseded_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
                (replaces_id,),
            )
            connection.execute(
                "UPDATE pst_imports SET is_active = 1 WHERE id = ?", (import_id,)
            )

        self._run_generation_change("activate PST generation", activate)

    def restore_generation(self, import_id: int) -> None:
        def restore(connection: sqlite3.Connection) -> None:
            target = connection.execute(
                "SELECT status, is_active FROM pst_imports WHERE id = ?", (import_id,)
            ).fetchone()
            if target is None or target[1]:
                raise ValueError("PST generation is not restorable")
            current = connection.execute(
                "SELECT id FROM pst_imports WHERE is_active = 1 AND id != ? "
                "ORDER BY id DESC LIMIT 1",
                (import_id,),
            ).fetchone()
            if current is None:
                raise ValueError("No active PST generation to replace")
            current_id = int(current[0])
            connection.execute(
                "UPDATE messages SET local_state = 'active', trashed_at = NULL "
                "WHERE id IN (SELECT message_row_id FROM pst_import_items WHERE import_id = ?)",
                (import_id,),
            )
            connection.execute(
                "UPDATE messages SET local_state = 'trashed', "
                "trashed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
                "WHERE id IN (SELECT message_row_id FROM pst_import_items WHERE import_id = ?)",
                (current_id,),
            )
            connection.execute(
                "UPDATE pst_imports SET is_active = 0, status = 'superseded', "
                "superseded_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
                (current_id,),
            )
            connection.execute(
                "UPDATE pst_imports SET is_active = 1, status = 'completed', "
                "superseded_at = NULL WHERE id = ?",
                (import_id,),
            )

        self._run_generation_change("restore PST generation", restore)
