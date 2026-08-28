"""Operational metadata database backups."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mail_dock.domain.errors import DatabaseError
from mail_dock.infrastructure.storage.detach import storage_io

LAST_BACKUP_STATE_KEY = "last_database_backup_at"
BACKUP_INTERVAL = timedelta(days=7)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def backup_is_due(last_backup_at: str | None, *, now: datetime | None = None) -> bool:
    """Return whether a weekly backup should be created."""

    if last_backup_at is None:
        return True
    try:
        recorded_at = datetime.fromisoformat(last_backup_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    current_time = _as_utc(now or datetime.now(UTC))
    return current_time - _as_utc(recorded_at) >= BACKUP_INTERVAL


def backup_timestamp(*, now: datetime | None = None) -> str:
    """Return the persisted UTC timestamp for a completed backup."""

    return _as_utc(now or datetime.now(UTC)).isoformat().replace("+00:00", "Z")


def local_backup_is_allowed(
    source_encryption: str,
    destination_encryption: str = "unknown",
) -> bool:
    """Whether the destination is at least as protected as the source.

    The local configuration directory has no portable encryption probe and is
    therefore treated as ``unknown`` by the caller. Unknown and unencrypted
    destinations are equivalent for this conservative comparison, while an
    encrypted source requires an encrypted destination declaration.
    """

    encryption_strength = {"unencrypted": 0, "unknown": 0, "encrypted": 1}
    source_strength = encryption_strength.get(source_encryption, 0)
    destination_strength = encryption_strength.get(destination_encryption, 0)
    return destination_strength >= source_strength


def backup_database(connection: sqlite3.Connection, destination: Path) -> None:
    """Create an integrity-checked, atomically replaced SQLite backup."""

    temporary_path: Path | None = None
    backup_connection: sqlite3.Connection | None = None
    try:
        with storage_io():
            destination.parent.mkdir(parents=True, exist_ok=True)
            file_descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.",
                suffix=".tmp",
                dir=destination.parent,
            )
            os.close(file_descriptor)
            temporary_path = Path(temporary_name)
            backup_connection = sqlite3.connect(temporary_path)
            connection.backup(backup_connection)
            integrity = backup_connection.execute("PRAGMA integrity_check").fetchone()
            if integrity != ("ok",):
                raise DatabaseError(f"Backup integrity check failed: {destination.name}")
            backup_connection.close()
            backup_connection = None
            temporary_path.replace(destination)
    except DatabaseError:
        raise
    except (OSError, sqlite3.Error) as error:
        raise DatabaseError(f"Could not back up the database to {destination.name}") from error
    finally:
        if backup_connection is not None:
            with suppress(sqlite3.Error):
                backup_connection.close()
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink()
