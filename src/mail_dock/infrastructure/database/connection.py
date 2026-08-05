"""SQLite connection management for the metadata database.

Connections are never shared between threads and ``check_same_thread=False``
is intentionally not used. Writes are collected behind a single writer, and
each owning thread must call :meth:`ConnectionManager.close_current_thread`
before it exits. This keeps connection cleanup cooperative instead of
closing a SQLite connection from a different thread.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import suppress
from pathlib import Path
from typing import cast
from urllib.parse import quote

from mail_dock.domain.errors import DatabaseError
from mail_dock.infrastructure.storage.detach import storage_io

BUSY_TIMEOUT_MS = 10_000
CACHE_SIZE_KIB = -64_000
JOURNAL_MODES = frozenset({"WAL", "DELETE"})


def _readonly_uri(db_path: Path) -> str:
    """Build a SQLite URI that opens an existing database without creating it."""

    return f"file:{quote(db_path.absolute().as_posix(), safe='/:')}?mode=ro"


def _configure_connection(
    connection: sqlite3.Connection,
    *,
    readonly: bool,
    journal_mode: str,
) -> None:
    """Apply connection-local and database journaling settings."""

    if journal_mode not in JOURNAL_MODES:
        choices = ", ".join(sorted(JOURNAL_MODES))
        raise DatabaseError(f"journal_mode must be one of: {choices}")

    # This must be set for every connection; SQLite does not inherit it from
    # another connection, and foreign-key enforcement is otherwise silently off.
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA temp_store = MEMORY")
    connection.execute(f"PRAGMA cache_size = {CACHE_SIZE_KIB}")
    connection.execute("PRAGMA synchronous = NORMAL")

    if readonly:
        connection.execute("PRAGMA query_only = ON")
        return

    # The caller selects this from the storage probe and DriveKind.
    connection.execute(f"PRAGMA journal_mode = {journal_mode}")


def connect(
    db_path: Path,
    *,
    readonly: bool = False,
    journal_mode: str = "WAL",
) -> sqlite3.Connection:
    """Open and configure a metadata database connection.

    Read-only connections use SQLite's ``mode=ro`` URI and never create a
    database file. They also skip journal-mode changes because even a pragma
    that looks like a query can modify the database journal.
    """

    connection: sqlite3.Connection | None = None
    try:
        with storage_io():
            if readonly:
                connection = sqlite3.connect(
                    _readonly_uri(db_path),
                    uri=True,
                    check_same_thread=True,
                    timeout=BUSY_TIMEOUT_MS / 1000,
                )
            else:
                connection = sqlite3.connect(
                    db_path,
                    check_same_thread=True,
                    timeout=BUSY_TIMEOUT_MS / 1000,
                )
            _configure_connection(
                connection,
                readonly=readonly,
                journal_mode=journal_mode,
            )
    except Exception:
        if connection is not None:
            with suppress(sqlite3.Error):
                connection.close()
        raise
    return connection


class ConnectionManager:
    """Own one SQLite connection per thread and coordinate shutdown.

    ``request_close_all`` prevents new connections while workers finish. It
    does not close another thread's connection; every owner must call
    ``close_current_thread`` and the composition root can then use
    ``assert_all_closed`` to detect a worker that failed to cooperate.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        readonly: bool = False,
        journal_mode: str = "WAL",
    ) -> None:
        self._db_path = db_path
        self._readonly = readonly
        self._journal_mode = journal_mode
        self._local = threading.local()
        self._state_lock = threading.Lock()
        self._owners: dict[int, threading.Thread] = {}
        self._close_requested = False

    def get_connection(self) -> sqlite3.Connection:
        """Return the calling thread's connection, creating it if allowed."""

        connection = cast(
            sqlite3.Connection | None,
            getattr(self._local, "connection", None),
        )
        if connection is not None:
            return connection

        with self._state_lock:
            if self._close_requested:
                raise DatabaseError("Database connection shutdown has been requested")
            connection = connect(
                self._db_path,
                readonly=self._readonly,
                journal_mode=self._journal_mode,
            )
            self._local.connection = connection
            self._owners[threading.get_ident()] = threading.current_thread()
        return connection

    def close_current_thread(self) -> None:
        """Close and unregister the connection owned by the calling thread."""

        connection = getattr(self._local, "connection", None)
        if connection is None:
            return

        try:
            with storage_io():
                connection.close()
        finally:
            del self._local.connection
            with self._state_lock:
                self._owners.pop(threading.get_ident(), None)

    def request_close_all(self) -> None:
        """Stop new connections while existing worker connections drain."""

        with self._state_lock:
            self._close_requested = True

    def assert_all_closed(self) -> None:
        """Raise when any owning thread failed to close its connection."""

        with self._state_lock:
            if self._owners:
                owners = ", ".join(
                    f"{thread.name} ({thread.ident})" for thread in self._owners.values()
                )
                raise AssertionError(f"SQLite connections remain open: {owners}")


def checkpoint_truncate(connection: sqlite3.Connection) -> None:
    """Truncate the WAL for a writable connection.

    Read-only connections are detected through their connection-local
    ``query_only`` setting and return without issuing a checkpoint.
    """

    with storage_io():
        query_only = connection.execute("PRAGMA query_only").fetchone()
        if query_only and bool(query_only[0]):
            return
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
