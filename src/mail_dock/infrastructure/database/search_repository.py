"""Read-only SQLite search repository.

This repository never opens a transaction. It uses the per-thread connection
provided by ``ConnectionManager`` so searches do not share connections with
other workers and do not add a write lock while synchronization is running.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from mail_dock.domain.errors import DatabaseError, OperationCancelledError
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.search import (
    BaseSearchRepository,
    MessageDetail,
    MessageFilter,
    MessageSummary,
    PageCursor,
    SearchPage,
    SearchPlan,
)
from mail_dock.infrastructure.database.connection import ConnectionManager
from mail_dock.infrastructure.storage.detach import classify_sqlite_error, storage_io


class SqliteSearchRepository(BaseSearchRepository):
    """Execute read-only searches using a connection or connection manager."""

    def __init__(self, connection: sqlite3.Connection | ConnectionManager) -> None:
        self._connection = connection if isinstance(connection, sqlite3.Connection) else None
        self._connection_manager = connection if isinstance(connection, ConnectionManager) else None
        if self._connection is None and self._connection_manager is None:
            raise TypeError("connection must be sqlite3.Connection or ConnectionManager")

    def _conn(self) -> sqlite3.Connection:
        if self._connection is not None:
            return self._connection
        assert self._connection_manager is not None
        return self._connection_manager.get_connection()

    @contextmanager
    def _db_io(self, operation: str, cancel: CancelToken | None = None) -> Iterator[None]:
        try:
            with storage_io():
                if cancel is not None:
                    cancel.raise_if_cancelled()
                yield
        except sqlite3.Error as error:
            if cancel is not None and cancel.is_cancelled:
                raise OperationCancelledError("operation cancelled") from error
            classified = classify_sqlite_error(error)
            if classified is not error:
                raise classified from error
            raise DatabaseError(f"SQLite operation failed: {operation}") from error

    def _install_progress_handler(self, cancel: CancelToken | None) -> None:
        if cancel is not None:
            self._conn().set_progress_handler(lambda: int(cancel.is_cancelled), 1_000)

    def _clear_progress_handler(self, cancel: CancelToken | None) -> None:
        if cancel is not None:
            self._conn().set_progress_handler(None, 0)

    @staticmethod
    def _term_query(term: str, *, like: bool) -> tuple[str, tuple[str, ...]]:
        if like:
            predicate = (
                "subject_norm LIKE ? ESCAPE '\\' OR "
                "sender_norm LIKE ? ESCAPE '\\' OR "
                "body_text LIKE ? ESCAPE '\\' OR "
                "attachment_names LIKE ? ESCAPE '\\'"
            )
            return (
                "SELECT message_id FROM message_contents WHERE " + predicate,
                tuple(f"%{term}%" for _ in range(4)),
            )
        return "SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?", (term,)

    @classmethod
    def _matching_expression(cls, plan: SearchPlan) -> tuple[str, list[str]]:
        positive_terms = [(term, False) for term in plan.match_terms]
        positive_terms.extend((term, True) for term in plan.like_terms)
        negative_terms = [(term, False) for term in plan.exclude_match_terms]
        negative_terms.extend((term, True) for term in plan.exclude_like_terms)

        def combine(terms: list[tuple[str, bool]], mode: str) -> tuple[str, list[str]]:
            expressions: list[str] = []
            parameters: list[str] = []
            for term, like in terms:
                expression, term_parameters = cls._term_query(term, like=like)
                expressions.append(expression)
                parameters.extend(term_parameters)
            if not expressions:
                return "", parameters
            operator = " UNION " if mode == "or" else " INTERSECT "
            return operator.join(expressions), parameters

        positive, parameters = combine(positive_terms, plan.mode)
        negative, negative_parameters = combine(negative_terms, "or")
        parameters.extend(negative_parameters)
        if negative:
            positive = f"{positive} EXCEPT {negative}" if positive else negative
        return positive, parameters

    @staticmethod
    def _filter_clause(filters: MessageFilter) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if filters.account_ids is not None:
            if not filters.account_ids:
                return ["0"], []
            clauses.append("m.account_id IN (" + ", ".join("?" for _ in filters.account_ids) + ")")
            parameters.extend(filters.account_ids)
        if filters.folder_ids is not None:
            if not filters.folder_ids:
                return ["0"], []
            clauses.append("m.folder_id IN (" + ", ".join("?" for _ in filters.folder_ids) + ")")
            parameters.extend(filters.folder_ids)
        if filters.date_from is not None:
            clauses.append("COALESCE(m.date_sent, m.internal_date, '') >= ?")
            parameters.append(_db_datetime(filters.date_from))
        if filters.date_to is not None:
            clauses.append("COALESCE(m.date_sent, m.internal_date, '') <= ?")
            parameters.append(_db_datetime(filters.date_to))
        if filters.has_attachment is not None:
            clauses.append("m.has_attachment = ?")
            parameters.append(int(filters.has_attachment))
        if not filters.local_states:
            return ["0"], []
        clauses.append("m.local_state IN (" + ", ".join("?" for _ in filters.local_states) + ")")
        parameters.extend(sorted(filters.local_states))
        if filters.remote_states is not None:
            if not filters.remote_states:
                return ["0"], []
            clauses.append(
                "m.remote_state IN (" + ", ".join("?" for _ in filters.remote_states) + ")"
            )
            parameters.extend(sorted(filters.remote_states))
        if filters.thread_key is not None:
            clauses.append("m.thread_key = ?")
            parameters.append(filters.thread_key)
        return clauses, parameters

    @staticmethod
    def _datetime(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None

    @classmethod
    def _summary(cls, row: sqlite3.Row | tuple[Any, ...]) -> MessageSummary:
        return MessageSummary(
            id=int(row[0]),
            account_id=str(row[1]),
            folder_id=int(row[2]),
            folder_raw_name=str(row[3]),
            folder_display_name=str(row[4]),
            subject=str(row[5] or ""),
            sender=str(row[6] or ""),
            date_sent=cls._datetime(row[7]),
            internal_date=cls._datetime(row[8]),
            size_bytes=int(row[9]) if row[9] is not None else None,
            has_attachment=bool(row[10]),
            remote_state=str(row[11]),
            local_state=str(row[12]),
            thread_key=str(row[13]) if row[13] is not None else None,
            imap_flags=str(row[14]) if row[14] is not None else None,
            moved_to_folder_display_name=(str(row[15]) if row[15] is not None else None),
            failure_class=str(row[16]) if row[16] is not None else None,
        )

    def _page(
        self,
        *,
        plan: SearchPlan | None,
        filters: MessageFilter,
        cursor: PageCursor | None,
        limit: int,
        cancel: CancelToken | None,
    ) -> SearchPage:
        if limit <= 0:
            raise ValueError("limit must be positive")
        match_expression, match_parameters = self._matching_expression(plan) if plan else ("", [])
        clauses, filter_parameters = self._filter_clause(filters)
        parameters: list[Any] = []
        if match_expression:
            clauses.insert(0, f"m.id IN ({match_expression})")
            parameters.extend(match_parameters)
        parameters.extend(filter_parameters)
        if cursor is not None:
            # The COALESCE key keeps Date-missing rows in the same total order.
            clauses.append("(COALESCE(m.date_sent, m.internal_date, ''), m.id) < (?, ?)")
            parameters.extend((cursor.sort_key, cursor.message_id))
        where = " AND ".join(clauses) if clauses else "1"
        sql = (
            "SELECT m.id, m.account_id, m.folder_id, f.raw_name, f.display_name, "
            "m.subject, m.sender, m.date_sent, m.internal_date, m.size_bytes, "
            "m.has_attachment, m.remote_state, m.local_state, m.thread_key, "
            "m.imap_flags, moved_to_f.display_name, sf.error_class "
            "FROM messages AS m JOIN folders AS f ON f.id = m.folder_id "
            "LEFT JOIN folders AS moved_to_f ON moved_to_f.id = m.moved_to_folder_id "
            "LEFT JOIN sync_failures AS sf ON sf.account_id = m.account_id "
            "AND sf.folder_id = m.folder_id AND sf.uidvalidity = m.uidvalidity "
            "AND sf.uid = m.uid "
            f"WHERE {where} "
            "ORDER BY COALESCE(m.date_sent, m.internal_date, '') DESC, m.id DESC LIMIT ?"
        )
        parameters.append(limit + 1)
        connection = self._conn()
        self._install_progress_handler(cancel)
        try:
            rows = connection.execute(sql, parameters).fetchall()
        finally:
            self._clear_progress_handler(cancel)
        summaries = tuple(self._summary(row) for row in rows[:limit])
        has_more = len(rows) > limit
        next_cursor = None
        if has_more and summaries:
            last = summaries[-1]
            next_cursor = PageCursor(_sort_key(last), last.id)
        return SearchPage(summaries, next_cursor, not has_more)

    def search_messages(
        self,
        plan: SearchPlan,
        filters: MessageFilter,
        *,
        cursor: PageCursor | None = None,
        limit: int = 200,
        cancel: CancelToken | None = None,
    ) -> SearchPage:
        with self._db_io("search messages", cancel):
            return self._page(plan=plan, filters=filters, cursor=cursor, limit=limit, cancel=cancel)

    def list_messages(
        self,
        filters: MessageFilter,
        *,
        cursor: PageCursor | None = None,
        limit: int = 200,
        cancel: CancelToken | None = None,
    ) -> SearchPage:
        with self._db_io("list messages", cancel):
            return self._page(plan=None, filters=filters, cursor=cursor, limit=limit, cancel=cancel)

    def count_messages(
        self,
        filters: MessageFilter,
        plan: SearchPlan | None = None,
        *,
        cancel: CancelToken | None = None,
    ) -> int:
        with self._db_io("count messages", cancel):
            match_expression, match_parameters = (
                self._matching_expression(plan) if plan else ("", [])
            )
            clauses, filter_parameters = self._filter_clause(filters)
            parameters: list[Any] = []
            if match_expression:
                clauses.insert(0, f"m.id IN ({match_expression})")
                parameters.extend(match_parameters)
            parameters.extend(filter_parameters)
            where = " AND ".join(clauses) if clauses else "1"
            self._install_progress_handler(cancel)
            try:
                row = (
                    self._conn()
                    .execute(
                        "SELECT COUNT(*) FROM messages AS m WHERE " + where,
                        parameters,
                    )
                    .fetchone()
                )
            finally:
                self._clear_progress_handler(cancel)
        return int(row[0]) if row is not None else 0

    def list_thread(
        self,
        thread_key: str,
        filters: MessageFilter,
        *,
        cancel: CancelToken | None = None,
    ) -> Sequence[MessageSummary]:
        thread_filters = MessageFilter(
            account_ids=filters.account_ids,
            folder_ids=filters.folder_ids,
            date_from=filters.date_from,
            date_to=filters.date_to,
            has_attachment=filters.has_attachment,
            local_states=filters.local_states,
            remote_states=filters.remote_states,
            thread_key=thread_key,
        )
        with self._db_io("list thread", cancel):
            return self._page(
                plan=None,
                filters=thread_filters,
                cursor=None,
                limit=2_147_483_647,
                cancel=cancel,
            ).items

    def get_message(self, message_id: int) -> MessageDetail | None:
        with self._db_io("get message"):
            row = (
                self._conn()
                .execute(
                    "SELECT m.id, m.account_id, m.folder_id, f.raw_name, f.display_name, "
                    "m.subject, m.sender, m.date_sent, m.internal_date, m.size_bytes, "
                    "m.has_attachment, m.remote_state, m.local_state, m.thread_key, "
                    "m.imap_flags, moved_to_f.display_name, sf.error_class, "
                    "m.recipient, m.cc, m.message_id, m.in_reply_to, m.references_ids, "
                    "m.relative_path, m.file_hash "
                    "FROM messages AS m JOIN folders AS f ON f.id = m.folder_id "
                    "LEFT JOIN folders AS moved_to_f ON moved_to_f.id = m.moved_to_folder_id "
                    "LEFT JOIN sync_failures AS sf ON sf.account_id = m.account_id "
                    "AND sf.folder_id = m.folder_id AND sf.uidvalidity = m.uidvalidity "
                    "AND sf.uid = m.uid WHERE m.id = ?",
                    (message_id,),
                )
                .fetchone()
            )
        if row is None:
            return None
        summary = self._summary(row)
        return MessageDetail(
            **summary.__dict__,
            recipient=str(row[17] or ""),
            cc=str(row[18] or ""),
            message_id=str(row[19]) if row[19] is not None else None,
            in_reply_to=str(row[20]) if row[20] is not None else None,
            references_ids=str(row[21]) if row[21] is not None else None,
            relative_path=str(row[22]) if row[22] is not None else None,
            file_hash=str(row[23]) if row[23] is not None else None,
        )


def _db_datetime(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(UTC)
    return value.isoformat().replace("+00:00", "Z")


def _sort_key(summary: MessageSummary) -> str:
    value = summary.date_sent or summary.internal_date
    return _db_datetime(value) if value is not None else ""
