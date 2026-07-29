"""The mail-dock composition root and command-line interface."""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from mail_dock import __version__, config
from mail_dock.domain.errors import (
    ConfigError,
    DatabaseError,
    MailDockError,
    StorageError,
    StorageForeignRootError,
    StorageLockedError,
    StorageRootMissingError,
)
from mail_dock.infrastructure.database.connection import ConnectionManager
from mail_dock.infrastructure.database.migrator import migrate
from mail_dock.infrastructure.logging_config import set_storage_log_target, setup_logging
from mail_dock.infrastructure.storage.detach import storage_io
from mail_dock.infrastructure.storage.storage_root import (
    DriveKind,
    RootProbe,
    RootResolution,
    StorageLock,
    check_free_space,
    drive_kind,
    ensure_layout,
    initialize_root,
    resolve_root,
)

LOGGER = logging.getLogger(__name__)


def _common_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    parser.add_argument("--storage-root", type=Path, help="storage root path")
    parser.add_argument("--debug", action="store_true", help="enable console debug logging")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def _build_parser() -> argparse.ArgumentParser:
    common = _common_parser()
    parser = argparse.ArgumentParser(
        prog="mail-dock",
        description="Local mail backup and viewing application",
        parents=[common],
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "migrate",
        parents=[common],
        help="apply pending database migrations and exit",
    )
    subparsers.add_parser(
        "verify",
        parents=[common],
        help="run read-only database integrity checks and exit",
    )
    return parser


def _select_root(
    settings: config.AppConfig,
    requested_root: Path | None,
) -> tuple[Path, str]:
    expected_uuid = settings.storage_root_uuid
    if requested_root is not None:
        candidate = requested_root.expanduser()
        resolution = resolve_root([candidate], expected_uuid)
        if resolution.probe is RootProbe.FOREIGN:
            raise StorageForeignRootError(f"Storage root belongs to another archive: {candidate}")
        if resolution.probe is RootProbe.MISSING:
            marker = initialize_root(candidate)
            confirmed = initialize_root(candidate)
            if confirmed.root_uuid != marker.root_uuid:
                raise StorageForeignRootError(f"Storage root marker changed: {candidate}")
            resolution = RootResolution(candidate.resolve(strict=False), RootProbe.OK)
        root = resolution.path
    else:
        resolution = resolve_root(
            [Path(candidate) for candidate in settings.storage_root_candidates],
            expected_uuid,
        )
        if resolution.probe is RootProbe.FOREIGN:
            foreign_path = resolution.path or Path("<unknown>")
            raise StorageForeignRootError(
                f"Storage root belongs to another archive: {foreign_path}"
            )
        root = resolution.path

    if root is None:
        raise StorageRootMissingError(
            "No mail-dock storage root was found; specify --storage-root on first run"
        )
    marker = initialize_root(root)
    if expected_uuid is not None and requested_root is None and marker.root_uuid != expected_uuid:
        raise StorageForeignRootError(f"Storage root UUID does not match: {root}")
    return root, marker.root_uuid


def _verify_database(connection: sqlite3.Connection) -> None:
    try:
        with storage_io():
            quick_check = connection.execute("PRAGMA quick_check").fetchone()
            foreign_key_violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.Error as error:
        raise DatabaseError("Database verification failed") from error
    if quick_check != ("ok",):
        raise DatabaseError("Database quick_check failed")
    if foreign_key_violations:
        raise DatabaseError("Database foreign_key_check failed")


def _close_manager(manager: ConnectionManager | None) -> None:
    if manager is None:
        return
    manager.request_close_all()
    manager.close_current_thread()
    manager.assert_all_closed()


def _run_command(
    settings: config.AppConfig,
    requested_root: Path | None,
    command: str | None,
) -> None:
    root, root_uuid = _select_root(settings, requested_root)
    lock = StorageLock(root, heartbeat_interval_sec=settings.heartbeat_interval_sec)
    manager: ConnectionManager | None = None
    storage_logging_enabled = False
    try:
        lock.acquire()
        ensure_layout(root)
        check_free_space(root)
        set_storage_log_target(root / "logs")
        storage_logging_enabled = True

        network_drive = drive_kind(root) is DriveKind.NETWORK
        readonly = command == "verify"
        manager = ConnectionManager(
            root / "metadata.db",
            readonly=readonly,
            network_drive=network_drive,
        )
        connection = manager.get_connection()
        if readonly:
            _verify_database(connection)
            LOGGER.info("Database verification succeeded")
        else:
            version = migrate(connection, root / "metadata.db")
            LOGGER.info("Database migration complete at schema version %d", version)

        updated_settings = replace(
            settings,
            storage_root_uuid=root_uuid,
            storage_root_candidates=(str(root.resolve(strict=False)),),
        )
        config.save(updated_settings)
    finally:
        try:
            _close_manager(manager)
        finally:
            try:
                if storage_logging_enabled:
                    set_storage_log_target(None)
            finally:
                lock.release()


def _exit_code(error: MailDockError) -> int:
    if isinstance(error, StorageLockedError):
        return 3
    if isinstance(error, DatabaseError):
        return 4
    if isinstance(error, ConfigError | StorageError):
        return 2
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    """Run the mail-dock command-line application."""

    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        settings = config.load()
        setup_logging(config.config_dir(), debug=bool(getattr(args, "debug", False)))
        _run_command(
            settings,
            getattr(args, "storage_root", None),
            getattr(args, "command", None),
        )
    except MailDockError as error:
        LOGGER.error("mail-dock stopped: %s", error)
        print(f"mail-dock: {error}", file=sys.stderr)
        return _exit_code(error)
    except OSError as error:
        LOGGER.error("mail-dock stopped during local I/O")
        print(f"mail-dock: local I/O failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
