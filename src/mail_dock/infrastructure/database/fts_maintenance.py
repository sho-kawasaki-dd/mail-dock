"""Maintenance checks for the external-content FTS5 index.

Phase 2 only exposes integrity checking. Rebuild and optimize operations
belong to the Phase 4 re-indexing workflow.

The integrity-check command is an SQL INSERT required by SQLite FTS5, but it
does not modify the original message data. Callers must provide a short-lived
writable connection dedicated to this check.
"""

from __future__ import annotations

import sqlite3

from mail_dock.domain.errors import DatabaseError
from mail_dock.infrastructure.storage.detach import classify_sqlite_error, storage_io


def integrity_check(conn: sqlite3.Connection) -> None:
    """Verify that the external-content FTS index matches message_contents.

    ``rank=1`` is required for external-content tables because it makes FTS5
    compare the index against the content table rather than only checking the
    index's internal structure.
    """

    try:
        with storage_io():
            conn.execute(
                """
                INSERT INTO messages_fts(messages_fts, rank)
                VALUES ('integrity-check', 1)
                """
            )
    except sqlite3.Error as error:
        classified = classify_sqlite_error(error)
        if classified is not error:
            raise classified from error
        raise DatabaseError("FTS integrity check failed") from error