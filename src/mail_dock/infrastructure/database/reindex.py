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
from mail_dock.infrastructure.database.pst_import_repository import SqlitePstImportRepository
from mail_dock.infrastructure.storage.detach import storage_io
from mail_dock.infrastructure.storage.pst_manifest import PstManifestReader
from mail_dock.usecases.reindex import ReindexProgress, ReindexResult, reindex, reindex_pst

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
        results.extend(
            _rebuild_pst_manifests(
                connection,
                storage,
                database_path.parent,
                cancel=cancel,
            )
        )
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


def _rebuild_pst_manifests(
    connection: sqlite3.Connection,
    storage: BaseEmlStorage,
    storage_root: Path,
    *,
    cancel: CancelToken | None,
) -> list[ReindexResult]:
    """Rebuild valid PST generations without guessing from orphaned files."""

    manifest_root = storage_root / "manifests" / "pst"
    if not manifest_root.is_dir():
        return []

    descriptors: dict[str, tuple[PstManifestReader, str | None]] = {}
    superseded_at: dict[str, str | None] = {}
    for directory in sorted(path for path in manifest_root.iterdir() if path.is_dir()):
        try:
            reader = PstManifestReader(storage_root, directory.name)
            snapshot = reader.read_import_manifest()
            import_uuid = snapshot.get("import_uuid")
            if not isinstance(import_uuid, str):
                raise ValueError("missing import_uuid")
            replaces_uuid: str | None = None
            for event in reader.read_all_events():
                if event.get("event") == "generation_switch_committed":
                    candidate = event.get("replaces_import_uuid")
                    if isinstance(candidate, str):
                        replaces_uuid = candidate
                        superseded_at[candidate] = (
                            event.get("timestamp")
                            if isinstance(event.get("timestamp"), str)
                            else None
                        )
                elif event.get("event") == "generation_restored":
                    candidate = event.get("superseded_import_uuid")
                    if isinstance(candidate, str):
                        superseded_at[candidate] = (
                            event.get("timestamp")
                            if isinstance(event.get("timestamp"), str)
                            else None
                        )
            descriptors[import_uuid] = (reader, replaces_uuid)
        except (OSError, TypeError, ValueError) as error:
            _LOGGER.warning(
                "Skipping unsupported PST manifests during reindex: path=%s error=%s",
                directory,
                error,
            )

    if not descriptors:
        return []

    repository = SqliteMessageRepository(connection)
    pst_repository = SqlitePstImportRepository(connection)
    pending = set(descriptors)
    rebuilt: dict[str, int] = {}
    superseded = set(superseded_at)
    results: list[ReindexResult] = []
    while pending:
        progress = False
        for import_uuid in sorted(pending):
            reader, replaces_uuid = descriptors[import_uuid]
            if replaces_uuid is not None and replaces_uuid not in rebuilt:
                if replaces_uuid not in descriptors:
                    _LOGGER.warning(
                        "Skipping PST generation with missing predecessor: %s", import_uuid
                    )
                    pending.remove(import_uuid)
                    progress = True
                continue
            is_active = import_uuid not in superseded
            try:
                result = reindex_pst(
                    repository,
                    pst_repository,
                    storage,
                    reader,
                    status="completed" if is_active else "superseded",
                    is_active=is_active,
                    replaces_id=rebuilt.get(replaces_uuid) if replaces_uuid else None,
                    superseded_at=superseded_at.get(import_uuid),
                    cancel=cancel,
                )
            except (OSError, TypeError, ValueError, StorageError) as error:
                _LOGGER.warning(
                    "Skipping unsupported PST manifests during reindex: uuid=%s error=%s",
                    import_uuid,
                    error,
                )
                pending.remove(import_uuid)
                progress = True
                continue
            results.append(result)
            row = connection.execute(
                "SELECT id FROM pst_imports WHERE import_uuid = ?", (import_uuid,)
            ).fetchone()
            if row is not None:
                rebuilt[import_uuid] = int(row[0])
            pending.remove(import_uuid)
            progress = True
        if not progress:
            _LOGGER.warning("Could not order PST generations for reindex: %s", sorted(pending))
            break
    return results


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
