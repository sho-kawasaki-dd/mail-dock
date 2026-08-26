"""Build and atomically install a replacement metadata database."""

from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from collections.abc import Callable, Iterable
from contextlib import suppress
from pathlib import Path

from mail_dock.domain.errors import DatabaseError, StorageError
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.ports import BaseEmlStorage, BaseManifestReader
from mail_dock.infrastructure.database.connection import checkpoint_truncate, connect
from mail_dock.infrastructure.database.fts_maintenance import integrity_check
from mail_dock.infrastructure.database.message_repository import SqliteMessageRepository
from mail_dock.infrastructure.database.migrator import migrate
from mail_dock.infrastructure.storage.detach import storage_io
from mail_dock.usecases.reindex import ReindexProgress, ReindexResult, reindex

_LOGGER = logging.getLogger(__name__)


def _fsync_parent(path: Path) -> None:
    if os.name == "nt":
        return
    directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _remove_temporary_database(path: Path) -> None:
    for sidecar in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        with suppress(OSError):
            sidecar.unlink(missing_ok=True)


def _verify_database(connection: sqlite3.Connection) -> None:
    try:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        foreign_key_check = connection.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.Error as error:
        raise DatabaseError("Rebuilt database verification failed") from error
    if quick_check != ("ok",) or foreign_key_check:
        raise DatabaseError("Rebuilt database failed SQLite integrity checks")
    integrity_check(connection)


def rebuild_database(
    database_path: Path,
    storage: BaseEmlStorage,
    manifest_readers: Iterable[BaseManifestReader],
    *,
    cancel: CancelToken | None = None,
    on_progress: Callable[[ReindexProgress], None] | None = None,
    journal_mode: str = "WAL",
) -> ReindexResult:
    """Rebuild and atomically replace ``database_path`` from durable sources.

    The temporary database is created beside the destination so replacement
    stays on one volume. The destination is untouched until every account has
    been rebuilt and the replacement has passed SQLite and FTS checks.
    """

    database_path = database_path.expanduser().resolve()
    temporary_path = database_path.with_name(f"{database_path.name}.reindex-{uuid.uuid4().hex}.tmp")
    connection: sqlite3.Connection | None = None
    results: list[ReindexResult] = []
    try:
        pst_manifest_root = database_path.parent / "manifests" / "pst"
        if pst_manifest_root.is_dir():
            _LOGGER.warning(
                "Skipping unsupported PST manifests during reindex: path=%s",
                pst_manifest_root,
            )
        connection = connect(temporary_path, journal_mode=journal_mode)
        migrate(connection, temporary_path)
        repository = SqliteMessageRepository(connection)
        for manifest_reader in manifest_readers:
            result = reindex(
                repository,
                storage,
                manifest_reader,
                cancel=cancel,
                on_progress=on_progress,
            )
            results.append(result)
            if result.cancelled:
                return _combine_results(results)
        checkpoint_truncate(connection)
        _verify_database(connection)
        checkpoint_truncate(connection)
        connection.close()
        connection = None
        with storage_io():
            os.replace(temporary_path, database_path)  # noqa: PTH105
        _fsync_parent(database_path)
    except (DatabaseError, StorageError):
        raise
    except sqlite3.Error as error:
        raise DatabaseError("Could not rebuild metadata database") from error
    except OSError as error:
        raise StorageError("Could not install rebuilt metadata database") from error
    finally:
        if connection is not None:
            connection.close()
        _remove_temporary_database(temporary_path)

    return _combine_results(results)


def _combine_results(results: list[ReindexResult]) -> ReindexResult:
    return ReindexResult(
        sum(result.account_count for result in results),
        sum(result.folder_count for result in results),
        sum(result.message_count for result in results),
        sum(result.contents_count for result in results),
        sum(result.purged_count for result in results),
        sum(result.skipped_count for result in results),
        tuple(warning for result in results for warning in result.warnings),
        any(result.cancelled for result in results),
    )


__all__ = ["rebuild_database"]
