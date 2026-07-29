"""Apply numbered SQLite migrations bundled with the application."""

from __future__ import annotations

import re
import sqlite3
from importlib import resources
from pathlib import Path
from typing import Final

from mail_dock.domain.errors import MigrationError, SchemaVersionTooNewError
from mail_dock.infrastructure.storage.detach import storage_io

_MIGRATION_NAME: Final[re.Pattern[str]] = re.compile(r"(?P<version>\d{3})_.+\.sql\Z")
_FORBIDDEN_SQL: Final[re.Pattern[str]] = re.compile(
    r"\b(?:BEGIN|COMMIT|ROLLBACK)\b|\bPRAGMA\s+user_version\b",
    re.IGNORECASE,
)
_SQL_COMMENT_OR_LITERAL: Final[re.Pattern[str]] = re.compile(
    r"--[^\n]*|/\*.*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"",
    re.DOTALL,
)
_TRIGGER_BLOCK: Final[re.Pattern[str]] = re.compile(
    r"\bCREATE\s+(?:TEMP\s+)?TRIGGER\b.*?\bBEGIN\b.*?\bEND\s*;",
    re.IGNORECASE | re.DOTALL,
)


def _migration_files() -> list[tuple[int, resources.abc.Traversable]]:
    """Return bundled SQL migrations in ascending version order."""

    migration_dir = resources.files("mail_dock").joinpath("migrations")
    migrations: list[tuple[int, resources.abc.Traversable]] = []
    seen_versions: set[int] = set()

    try:
        entries = migration_dir.iterdir()
        for entry in entries:
            match = _MIGRATION_NAME.fullmatch(entry.name)
            if match is None or not entry.is_file():
                continue
            version = int(match.group("version"))
            if version in seen_versions:
                raise MigrationError(f"Duplicate migration version: {version}")
            seen_versions.add(version)
            migrations.append((version, entry))
    except MigrationError:
        raise
    except (OSError, TypeError) as error:
        raise MigrationError("Could not enumerate database migrations") from error

    migrations.sort(key=lambda migration: migration[0])
    return migrations


def _read_migration(path: resources.abc.Traversable) -> str:
    """Read and validate one migration's SQL policy."""

    try:
        sql = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise MigrationError(f"Could not read migration {path.name}") from error

    without_comments_or_literals = _SQL_COMMENT_OR_LITERAL.sub(" ", sql)
    without_trigger_bodies = _TRIGGER_BLOCK.sub(" ", without_comments_or_literals)
    if _FORBIDDEN_SQL.search(without_trigger_bodies):
        raise MigrationError(
            f"Migration {path.name} must not contain transaction statements or user_version"
        )
    return sql


def current_version(conn: sqlite3.Connection) -> int:
    """Return SQLite's current schema version."""

    try:
        with storage_io():
            row = conn.execute("PRAGMA user_version").fetchone()
    except sqlite3.Error as error:
        raise MigrationError("Could not read the database schema version") from error

    if row is None or not isinstance(row[0], int):
        raise MigrationError("SQLite returned an invalid schema version")
    return row[0]


def _database_is_nonempty(conn: sqlite3.Connection) -> bool:
    try:
        with storage_io():
            row = conn.execute(
                """
				SELECT EXISTS(
					SELECT 1
					FROM sqlite_master
					WHERE name NOT LIKE 'sqlite_%'
				)
				"""
            ).fetchone()
    except sqlite3.Error as error:
        raise MigrationError("Could not inspect the existing database") from error
    return bool(row and row[0])


def _next_backup_path(db_path: Path, version: int) -> Path:
    """Choose a non-destructive backup path for the pre-migration database."""

    base = db_path.with_name(f"{db_path.name}.bak.{version}")
    candidate = base
    suffix = 1
    while candidate.exists():
        candidate = db_path.with_name(f"{db_path.name}.bak.{version}.{suffix}")
        suffix += 1
    return candidate


def _backup_database(conn: sqlite3.Connection, db_path: Path, version: int) -> None:
    backup_path = _next_backup_path(db_path, version)
    backup_conn: sqlite3.Connection | None = None
    try:
        with storage_io():
            backup_conn = sqlite3.connect(backup_path)
            conn.backup(backup_conn)
            integrity = backup_conn.execute("PRAGMA integrity_check").fetchone()
            if integrity != ("ok",):
                raise MigrationError(f"Backup integrity check failed: {backup_path.name}")
            backup_conn.close()
            backup_conn = None
    except MigrationError:
        raise
    except sqlite3.Error as error:
        raise MigrationError(f"Could not back up the database to {backup_path.name}") from error
    finally:
        if backup_conn is not None:
            with storage_io():
                backup_conn.close()


def _apply_migration(conn: sqlite3.Connection, version: int, sql: str) -> None:
    script = f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;"
    try:
        with storage_io():
            conn.executescript(script)
    except sqlite3.Error as error:
        with storage_io():
            conn.rollback()
        raise MigrationError(f"Migration {version:03d} failed") from error


def migrate(conn: sqlite3.Connection, db_path: Path) -> int:
    """Apply all pending migrations and return the resulting schema version."""

    migrations = _migration_files()
    latest_version = migrations[-1][0] if migrations else 0
    version = current_version(conn)
    if version > latest_version:
        raise SchemaVersionTooNewError(
            f"Database schema version {version} is newer than supported version {latest_version}"
        )

    pending = [
        (migration_version, path)
        for migration_version, path in migrations
        if migration_version > version
    ]
    if not pending:
        return version

    if _database_is_nonempty(conn):
        _backup_database(conn, db_path, version)

    try:
        with storage_io():
            conn.execute("PRAGMA foreign_keys = OFF")
        for migration_version, path in pending:
            _apply_migration(conn, migration_version, _read_migration(path))
    except (MigrationError, SchemaVersionTooNewError):
        raise
    except sqlite3.Error as error:
        raise MigrationError("Database migration failed") from error
    finally:
        try:
            with storage_io():
                conn.execute("PRAGMA foreign_keys = ON")
        except sqlite3.Error as error:
            raise MigrationError("Could not restore SQLite foreign-key enforcement") from error

    try:
        with storage_io():
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.Error as error:
        raise MigrationError("Could not validate foreign-key constraints") from error
    if violations:
        raise MigrationError("Database migration left foreign-key violations")
    return current_version(conn)


# Phase 5 migrates messages.folder_id to the message_folders junction table.
