"""The mail-dock composition root and command-line interface."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import signal
import sqlite3
import sys
from collections.abc import Iterator, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

from mail_dock import __version__, config
from mail_dock.domain.errors import (
    AuthenticationError,
    ConfigError,
    CredentialStoreError,
    DatabaseError,
    FetchError,
    MailDockError,
    OperationCancelledError,
    SearchQueryError,
    StorageError,
    StorageForeignRootError,
    StorageLockedError,
    StorageRootMissingError,
    StorageUnsupportedError,
)
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.ports import BaseCredentialStore, BaseIntegrityStorage
from mail_dock.domain.repository import MessageRecord
from mail_dock.domain.search import MessageFilter, MessageSummary, PageCursor, SearchPage
from mail_dock.domain.storage_state import StorageStateMachine
from mail_dock.infrastructure.database.backup import (
    LAST_BACKUP_STATE_KEY,
    backup_database,
    backup_is_due,
    backup_timestamp,
    local_backup_is_allowed,
)
from mail_dock.infrastructure.database.connection import (
    ConnectionManager,
    checkpoint_truncate,
    connect,
)
from mail_dock.infrastructure.database.fts_maintenance import integrity_check
from mail_dock.infrastructure.database.message_repository import SqliteMessageRepository
from mail_dock.infrastructure.database.migrator import migrate
from mail_dock.infrastructure.database.search_repository import SqliteSearchRepository
from mail_dock.infrastructure.fetchers.onamae_imap import OnamaeImapFetcher
from mail_dock.infrastructure.logging_config import (
    purge_old_logs,
    set_storage_log_target,
    setup_logging,
)
from mail_dock.infrastructure.security.keyring_store import (
    KeyringBackendStatus,
    KeyringCredentialStore,
    detect_backend,
)
from mail_dock.infrastructure.security.session_store import SessionCredentialStore
from mail_dock.infrastructure.storage.capabilities import (
    CapabilityLevel,
    StorageCapabilities,
    capability_level,
    journal_mode_for,
    probe_capabilities,
    storage_fingerprint,
)
from mail_dock.infrastructure.storage.detach import storage_io
from mail_dock.infrastructure.storage.eml_storage import EmlStorage, cleanup_tmp
from mail_dock.infrastructure.storage.manifest import ManifestReader, ManifestWriter
from mail_dock.infrastructure.storage.storage_root import (
    DriveKind,
    RootProbe,
    RootResolution,
    StorageLock,
    check_free_space,
    drive_kind,
    ensure_layout,
    resolve_root,
)
from mail_dock.infrastructure.storage.storage_root import initialize_root as initialize_root
from mail_dock.usecases.register_account import (
    list_accounts,
    load_credentials,
    register_account,
)
from mail_dock.usecases.reindex import ReindexProgress, reindex
from mail_dock.usecases.reparse import reparse_messages
from mail_dock.usecases.search_messages import search_messages
from mail_dock.usecases.search_query import parse_query
from mail_dock.usecases.snapshots import (
    backfill_snapshots,
    recover_after_unclean_shutdown,
    repair_manifest_tails,
)
from mail_dock.usecases.sync_folders import refresh_folders, set_sync_target
from mail_dock.usecases.sync_mail import SyncOptions, SyncProgress, sync_account
from mail_dock.usecases.verify import (
    FullVerifyResult,
    ManifestVerifyResult,
    OrphanScanResult,
    QuickVerifyResult,
    RangeVerifyResult,
    VerifyProgress,
    full_verify,
    orphan_scan,
    quick_verify,
    range_verify,
    verify_manifest,
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


def _positive_limit(value: str) -> int:
    limit = int(value)
    if limit <= 0:
        raise argparse.ArgumentTypeError("limit must be positive")
    return limit


def _parse_cli_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as error:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from error


def _date_start(value: date | None) -> datetime | None:
    return datetime.combine(value, time.min, tzinfo=UTC) if value is not None else None


def _date_end(value: date | None) -> datetime | None:
    return datetime.combine(value, time.max, tzinfo=UTC) if value is not None else None


def _build_parser() -> argparse.ArgumentParser:
    # CLI intentionally has no option or environment variable for capability acknowledgement.
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
    verify_parser = subparsers.add_parser(
        "verify",
        parents=[common],
        help="run integrity verification and exit",
    )
    verify_parser.add_argument(
        "--mode",
        choices=("quick", "range", "full", "orphans", "manifest"),
        default="quick",
        help="verification mode (default: quick)",
    )
    verify_parser.add_argument(
        "--account",
        help="limit full/orphan verification to an account",
    )
    reindex_parser = subparsers.add_parser(
        "reindex",
        parents=[common],
        help="rebuild the metadata database from manifests and EML files",
    )
    reindex_parser.add_argument("--account", help="only rebuild this account")
    subparsers.add_parser(
        "gui",
        parents=[common],
        help="start the graphical application",
    )
    account_parser = subparsers.add_parser(
        "account",
        parents=[common],
        help="manage registered mail accounts",
    )
    account_subparsers = account_parser.add_subparsers(dest="account_command", required=True)
    account_add = account_subparsers.add_parser(
        "add",
        parents=[common],
        help="register an IMAP account",
    )
    account_add.add_argument("--account-id", required=True, help="stable local account id")
    account_add.add_argument("--host", required=True, help="IMAP server hostname")
    account_add.add_argument("--port", type=int, default=993, help="IMAPS port")
    account_add.add_argument("--username", required=True, help="IMAP username")
    account_add.add_argument("--display-name", help="optional display name")
    account_subparsers.add_parser(
        "list",
        parents=[common],
        help="list registered accounts",
    )

    folders_parser = subparsers.add_parser(
        "folders",
        parents=[common],
        help="list and choose synchronization folders",
    )
    folders_parser.add_argument("--account", required=True, help="account id")
    folders_parser.add_argument(
        "--refresh",
        action="store_true",
        help="refresh folder metadata from the IMAP server",
    )
    folder_action = folders_parser.add_mutually_exclusive_group()
    folder_action.add_argument("--enable", metavar="RAW_NAME", help="enable a folder")
    folder_action.add_argument("--disable", metavar="RAW_NAME", help="disable a folder")

    sync_parser = subparsers.add_parser(
        "sync",
        parents=[common],
        help="synchronize enabled folders",
    )
    sync_parser.add_argument("--account", help="only synchronize this account")

    reparse_parser = subparsers.add_parser(
        "reparse",
        parents=[common],
        help="rebuild searchable message contents from stored EML files",
    )
    reparse_parser.add_argument("--account", help="only reparse this account")
    reparse_parser.add_argument(
        "--all",
        action="store_true",
        help="reparse all stored messages instead of failed messages only",
    )
    search_parser = subparsers.add_parser(
        "search",
        parents=[common],
        help="search stored messages",
    )
    search_parser.add_argument("query", help="search query")
    search_parser.add_argument(
        "--account",
        action="append",
        dest="accounts",
        metavar="ACCOUNT_ID",
        help="limit results to an account; may be repeated",
    )
    search_parser.add_argument(
        "--folder",
        action="append",
        dest="folders",
        metavar="RAW_NAME",
        help="limit results to a folder; may be repeated",
    )
    search_parser.add_argument("--since", type=_parse_cli_date, help="earliest date (YYYY-MM-DD)")
    search_parser.add_argument("--until", type=_parse_cli_date, help="latest date (YYYY-MM-DD)")
    attachment_action = search_parser.add_mutually_exclusive_group()
    attachment_action.add_argument(
        "--has-attachment",
        action="store_true",
        dest="has_attachment",
        help="only messages with attachments",
    )
    attachment_action.add_argument(
        "--no-attachment",
        action="store_false",
        dest="has_attachment",
        help="only messages without attachments",
    )
    search_parser.set_defaults(has_attachment=None)
    search_parser.add_argument(
        "--mode",
        choices=("and", "or"),
        default="and",
        help="combine query terms with AND or OR",
    )
    search_parser.add_argument(
        "--limit",
        type=_positive_limit,
        default=50,
        help="maximum number of results (default: 50)",
    )
    search_parser.add_argument("--after", help="continue after a previous next_cursor")
    search_parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
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


def _normalized_storage_path(root: Path) -> str:
    return os.path.normcase(str(root.expanduser().resolve(strict=False)))


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


def _verify_fts_database(db_path: Path, *, journal_mode: str) -> None:
    """Run the FTS check on a short-lived writable connection only."""

    connection = connect(db_path, journal_mode=journal_mode)
    try:
        integrity_check(connection)
    finally:
        connection.close()


def _close_manager(manager: ConnectionManager | None) -> None:
    if manager is None:
        return
    manager.request_close_all()
    manager.close_current_thread()
    manager.assert_all_closed()


class StorageSession:
    """Own the resources shared by CLI commands and the GUI.

    Root resolution happens before the lock is acquired, so the GUI can use
    the same root-selection function without starting a session during its
    first-run bootstrap. Once entered, this object owns the lock and
    connection manager until it is closed.
    """

    def __init__(
        self,
        settings: config.AppConfig,
        requested_root: Path | None,
        *,
        readonly: bool = False,
    ) -> None:
        self.settings = settings
        self.requested_root = requested_root
        self.readonly = readonly
        self._root: Path | None = None
        self.root_uuid: str | None = None
        self.network_drive = False
        self.journal_mode: str | None = None
        self.capabilities: StorageCapabilities | None = None
        self.capability_level: CapabilityLevel | None = None
        self.encryption_declaration = "unknown"
        self.credential_storage_mode: str | None = None
        self.credential_store: BaseCredentialStore | None = None
        self.manager: ConnectionManager | None = None
        self._lock: StorageLock | None = None
        self.recovery_results: tuple[Any, ...] = ()
        self.previous_clean_shutdown: bool | None = None
        self._clean_shutdown_written = False
        self._storage_logging_enabled = False
        self._entered = False
        self._closed = False

    def __enter__(self) -> StorageSession:
        if self._entered:
            raise RuntimeError("StorageSession cannot be entered more than once")

        root, root_uuid = _select_root(self.settings, self.requested_root)
        lock = StorageLock(root, heartbeat_interval_sec=self.settings.heartbeat_interval_sec)
        self._root = root
        self.root_uuid = root_uuid
        self.network_drive = drive_kind(root) is DriveKind.NETWORK
        self._lock = lock
        try:
            lock.acquire()
            ensure_layout(root)
            cleanup_tmp(root)
            checked_path = _normalized_storage_path(root)
            fingerprint = storage_fingerprint(root)
            profile = self._profile(root_uuid)
            cached = self._cached_capabilities(profile, checked_path, fingerprint)
            if cached is None:
                capabilities = probe_capabilities(root)
                level = capability_level(capabilities)
                self._persist_capabilities(
                    root_uuid,
                    capabilities,
                    level,
                    checked_path,
                    fingerprint,
                )
                profile = self._profile(root_uuid)
            else:
                capabilities, level = cached
            self.capabilities = capabilities
            self.capability_level = level
            self.encryption_declaration = self._encryption_from_profile(profile)
            capability_ack_at = profile.get("capability_ack_at") if profile else None
            if level is CapabilityLevel.UNSUPPORTED and not capability_ack_at:
                raise StorageUnsupportedError(root_uuid, level.value)
            check_free_space(root)
            set_storage_log_target(root / "logs")
            self._storage_logging_enabled = True
            journal_mode = journal_mode_for(capabilities, network_drive=self.network_drive)
            self.journal_mode = journal_mode
            self.credential_store = self._create_credential_store()

            self.manager = ConnectionManager(
                root / "metadata.db",
                readonly=self.readonly,
                journal_mode=self.journal_mode,
            )
            connection = self.manager.get_connection()
            if self.readonly:
                self.previous_clean_shutdown = self._read_clean_shutdown(connection)
                _verify_database(connection)
                self.manager.close_current_thread()
                _verify_fts_database(
                    root / "metadata.db",
                    journal_mode=journal_mode,
                )
                LOGGER.info("Database verification succeeded")
            else:
                version = migrate(connection, root / "metadata.db")
                LOGGER.info("Database migration complete at schema version %d", version)
                self.previous_clean_shutdown = self._read_clean_shutdown(connection)
                self._write_clean_shutdown(connection, False)
                self._backup_if_due(connection)
            if not self.readonly:
                removed_logs = purge_old_logs(root / "logs", self.settings.sync_log_retention_days)
                if removed_logs:
                    LOGGER.info("Removed %d expired synchronization logs", removed_logs)
            self._entered = True
            return self
        except BaseException:
            self._cleanup()
            raise

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        del exc_value, traceback
        try:
            if exc_type is None:
                self._save_settings()
                if not self.readonly:
                    connection = self.connection_manager.get_connection()
                    self._backup_if_due(connection, force=True)
                    if not self._clean_shutdown_written:
                        self._write_clean_shutdown(connection, True)
        finally:
            self._cleanup()

    @property
    def was_unclean_shutdown(self) -> bool:
        """Whether the previous writable session did not reach normal exit."""

        return self.previous_clean_shutdown is False

    @staticmethod
    def _read_clean_shutdown(connection: sqlite3.Connection) -> bool | None:
        try:
            row = connection.execute(
                "SELECT value FROM app_state WHERE key = ?",
                ("clean_shutdown",),
            ).fetchone()
        except sqlite3.Error as error:
            raise DatabaseError("Could not read clean shutdown state") from error
        if row is None:
            return None
        if row[0] == "1":
            return True
        if row[0] == "0":
            return False
        raise DatabaseError("Clean shutdown state is invalid")

    @staticmethod
    def _write_clean_shutdown(connection: sqlite3.Connection, clean: bool) -> None:
        try:
            connection.execute(
                "INSERT INTO app_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("clean_shutdown", "1" if clean else "0"),
            )
            connection.commit()
        except sqlite3.Error as error:
            raise DatabaseError("Could not write clean shutdown state") from error

    def _backup_if_due(self, connection: sqlite3.Connection, *, force: bool = False) -> None:
        """Create operational backups while the session still owns its connection."""

        if self._root is None or self.readonly:
            return
        last_backup_at = self._read_app_state(connection, LAST_BACKUP_STATE_KEY)
        if not force and not backup_is_due(last_backup_at):
            return

        try:
            backup_database(connection, self._root / "metadata.db.bak")
            if self.settings.db_backup_to_local_disk:
                if not local_backup_is_allowed(self.encryption_declaration):
                    LOGGER.warning(
                        "Skipping local database backup because the destination encryption "
                        "is weaker or unknown for an encrypted storage root"
                    )
                else:
                    backup_database(connection, config.config_dir() / "metadata.db.bak")
            self._write_app_state(connection, LAST_BACKUP_STATE_KEY, backup_timestamp())
            LOGGER.info("Database backup completed")
        except DatabaseError:
            LOGGER.exception("Database backup failed")

    @staticmethod
    def _read_app_state(connection: sqlite3.Connection, key: str) -> str | None:
        try:
            row = connection.execute(
                "SELECT value FROM app_state WHERE key = ?",
                (key,),
            ).fetchone()
        except sqlite3.Error as error:
            raise DatabaseError(f"Could not read application state: {key}") from error
        return None if row is None else row[0]

    @staticmethod
    def _write_app_state(connection: sqlite3.Connection, key: str, value: str) -> None:
        try:
            connection.execute(
                "INSERT INTO app_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            connection.commit()
        except sqlite3.Error as error:
            raise DatabaseError(f"Could not write application state: {key}") from error

    def checkpoint_for_detach(self) -> None:
        """Flush the WAL and close this thread's connection before root release."""

        if self.readonly:
            return
        manager = self.connection_manager
        connection = manager.get_connection()
        try:
            checkpoint_truncate(connection)
            self._write_clean_shutdown(connection, True)
            self._clean_shutdown_written = True
        finally:
            manager.request_close_all()
            manager.close_current_thread()
        manager.assert_all_closed()

    @staticmethod
    def _encryption_from_profile(profile: dict[str, config.JSONValue] | None) -> str:
        if profile is None:
            return "unknown"
        encryption = profile.get("encryption")
        return encryption if isinstance(encryption, str) else "unknown"

    def _profile(self, root_uuid: str) -> dict[str, config.JSONValue] | None:
        raw_profile = self.settings.storage_profiles.get(root_uuid)
        if not isinstance(raw_profile, dict):
            return None
        return dict(raw_profile)

    @staticmethod
    def _cached_capabilities(
        profile: dict[str, config.JSONValue] | None,
        checked_path: str,
        fingerprint: str,
    ) -> tuple[StorageCapabilities, CapabilityLevel] | None:
        if profile is None:
            return None
        if profile.get("checked_path") != checked_path:
            return None
        if profile.get("storage_fingerprint") != fingerprint:
            return None
        raw_capabilities = profile.get("capabilities")
        raw_level = profile.get("capability_level")
        if not isinstance(raw_capabilities, dict) or not isinstance(raw_level, str):
            return None
        capabilities = StorageCapabilities.from_dict(raw_capabilities)
        if capabilities is None:
            return None
        try:
            level = CapabilityLevel(raw_level)
        except ValueError:
            return None
        if capability_level(capabilities) is not level:
            return None
        return capabilities, level

    def _persist_capabilities(
        self,
        root_uuid: str,
        capabilities: StorageCapabilities,
        level: CapabilityLevel,
        checked_path: str,
        fingerprint: str,
    ) -> None:
        profiles = dict(self.settings.storage_profiles)
        profile = self._profile(root_uuid) or {}
        profile.update(
            {
                "capabilities": capabilities.as_dict(),
                "capability_level": level.value,
                "checked_path": checked_path,
                "storage_fingerprint": fingerprint,
            }
        )
        profiles[root_uuid] = profile
        updated_settings = replace(
            self.settings,
            storage_root_uuid=root_uuid,
            storage_root_candidates=(checked_path,),
            storage_profiles=profiles,
        )
        config.save(updated_settings)
        self.settings = updated_settings

    def _create_credential_store(self) -> BaseCredentialStore:
        if self.settings.credential_storage == "session_only":
            LOGGER.warning("Using session-only credential storage by configuration")
            self.credential_storage_mode = "session_only"
            return SessionCredentialStore()
        backend_status = detect_backend()
        if backend_status is KeyringBackendStatus.SUPPORTED:
            self.credential_storage_mode = "keyring"
            return KeyringCredentialStore()
        LOGGER.warning(
            "Keyring backend is %s; falling back to session-only credential storage",
            backend_status.value,
        )
        self.credential_storage_mode = "session_only"
        return SessionCredentialStore()

    def _save_settings(self) -> None:
        if self._root is None or self.root_uuid is None:
            return
        current_path = str(self._root.resolve(strict=False))
        current_normalized_path = _normalized_storage_path(self._root)
        profiles = {
            profile_uuid: profile
            for profile_uuid, profile in self.settings.storage_profiles.items()
            if isinstance(profile, dict) and profile.get("checked_path") == current_normalized_path
        }
        updated_settings = replace(
            self.settings,
            storage_root_uuid=self.root_uuid,
            storage_root_candidates=(current_path,),
            storage_profiles=profiles,
        )
        config.save(updated_settings)
        self.settings = updated_settings

    @property
    def root(self) -> Path:
        """Return the resolved root while the session is active."""

        if self._root is None or self._closed:
            raise RuntimeError("StorageSession is not active")
        return self._root

    def _cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            _close_manager(self.manager)
        finally:
            try:
                if self._storage_logging_enabled:
                    set_storage_log_target(None)
            finally:
                if self._lock is not None:
                    self._lock.release()

    @property
    def connection_manager(self) -> ConnectionManager:
        """Return the active manager for repository construction."""

        if self.manager is None or not self._entered or self._closed:
            raise RuntimeError("StorageSession is not active")
        return self.manager

    @property
    def active_credential_store(self) -> BaseCredentialStore:
        """Return the credential store selected for this session."""

        if self.credential_store is None or not self._entered or self._closed:
            raise RuntimeError("StorageSession is not active")
        return self.credential_store

    @property
    def storage_lock(self) -> StorageLock:
        """Return the active lock borrowed by the GUI composition context."""

        if self._lock is None or not self._entered or self._closed:
            raise RuntimeError("StorageSession is not active")
        return self._lock


def _account_by_id(repo: SqliteMessageRepository, account_id: str) -> MessageRecord:
    for account in repo.list_accounts():
        if account.get("id") == account_id:
            return account
    raise DatabaseError(f"Account does not exist: {account_id}")


def _account_id(account: MessageRecord) -> str:
    value = account.get("id")
    if not isinstance(value, str) or not value:
        raise DatabaseError("Account record has no valid id")
    return value


def _account_fetcher(
    account: MessageRecord,
    credential_store: BaseCredentialStore,
    settings: config.AppConfig,
) -> OnamaeImapFetcher:
    account_id = _account_id(account)
    host = account.get("host")
    username = account.get("username")
    port = account.get("port", 993)
    if not isinstance(host, str) or not host:
        raise ConfigError(f"Account has no valid host: {account_id}")
    if not isinstance(username, str) or not username:
        raise ConfigError(f"Account has no valid username: {account_id}")
    if isinstance(port, bool) or not isinstance(port, int) or port <= 0:
        raise ConfigError(f"Account has no valid port: {account_id}")
    return OnamaeImapFetcher(
        host,
        username,
        _load_cli_credentials(credential_store, account_id),
        port=port,
        remote_trash_folder=settings.remote_trash_folder,
    )


def _load_cli_credentials(credential_store: BaseCredentialStore, account_id: str) -> str:
    try:
        return load_credentials(credential_store, account_id)
    except AuthenticationError:
        if not isinstance(credential_store, SessionCredentialStore):
            raise
        try:
            password = getpass.getpass("IMAP password: ")
        except (EOFError, OSError) as error:
            raise CredentialStoreError("Credentials are required for this operation") from error
        credential_store.set_password(account_id, password)
        return password


def _format_bytes(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}" if unit != "B" else f"{value} B"
        amount /= 1024
    return f"{value} B"


def _format_eta(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.0f}s"


def _print_sync_progress(progress: SyncProgress) -> None:
    estimate = (
        _format_bytes(progress.total_bytes_estimate)
        if progress.total_bytes_estimate > 0
        else "unknown"
    )
    print(
        f"{progress.current_folder}: {_format_bytes(progress.transferred_bytes)} / "
        f"{estimate}, messages={progress.message_count}, eta={_format_eta(progress.eta_seconds)}"
    )


def _install_cancel_handler(token: CancelToken) -> Any:
    previous = signal.getsignal(signal.SIGINT)
    interrupt_count = 0

    def handle_interrupt(signum: int, frame: Any) -> None:
        del signum, frame
        nonlocal interrupt_count
        interrupt_count += 1
        if interrupt_count == 1:
            print("Cancellation requested; stopping at the next batch boundary.", file=sys.stderr)
            token.cancel()
            return
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, handle_interrupt)
    return previous


def _restore_cancel_handler(previous: Any) -> None:
    signal.signal(signal.SIGINT, previous)


def _print_accounts(accounts: Sequence[MessageRecord]) -> None:
    for account in accounts:
        account_id = account.get("id", "")
        display_name = account.get("display_name") or ""
        host = account.get("host", "")
        port = account.get("port", "")
        username = account.get("username", "")
        print(f"{account_id}\t{display_name}\t{username}@{host}:{port}")


def _print_folders(folders: Sequence[MessageRecord]) -> None:
    for folder in folders:
        enabled = "enabled" if bool(folder.get("is_sync_target", 0)) else "disabled"
        print(f"{folder.get('raw_name', '')}\t{folder.get('display_name', '')}\t{enabled}")


def _search_folder_ids(
    repo: SqliteMessageRepository,
    account_ids: tuple[str, ...] | None,
    raw_names: Sequence[str] | None,
) -> tuple[int, ...] | None:
    if raw_names is None:
        return None
    accounts = (
        account_ids
        if account_ids is not None
        else tuple(_account_id(account) for account in repo.list_accounts())
    )
    requested_names = frozenset(raw_names)
    folder_ids: list[int] = []
    for account_id in accounts:
        for folder in repo.list_folders(account_id):
            if folder.get("raw_name") in requested_names:
                folder_id = folder.get("id")
                if isinstance(folder_id, int):
                    folder_ids.append(folder_id)
    return tuple(folder_ids)


def _summary_json(summary: MessageSummary) -> dict[str, object]:
    return {
        "id": summary.id,
        "account_id": summary.account_id,
        "folder_id": summary.folder_id,
        "folder_raw_name": summary.folder_raw_name,
        "folder_display_name": summary.folder_display_name,
        "subject": summary.subject,
        "sender": summary.sender,
        "date_sent": summary.date_sent.isoformat() if summary.date_sent else None,
        "internal_date": summary.internal_date.isoformat() if summary.internal_date else None,
        "size_bytes": summary.size_bytes,
        "has_attachment": summary.has_attachment,
        "remote_state": summary.remote_state,
        "local_state": summary.local_state,
        "thread_key": summary.thread_key,
    }


def _print_search_page(page: SearchPage, *, as_json: bool) -> None:
    if as_json:
        payload = {
            "items": [_summary_json(item) for item in page.items],
            "next_cursor": page.next_cursor.to_string() if page.next_cursor else None,
            "exhausted": page.exhausted,
        }
        print(json.dumps(payload, ensure_ascii=False))
        return
    for item in page.items:
        date_value = item.date_sent or item.internal_date
        date_text = date_value.isoformat() if date_value else ""
        folder_text = item.folder_display_name or item.folder_raw_name
        size_text = _format_bytes(item.size_bytes) if item.size_bytes is not None else ""
        print(
            f"{date_text}\t{item.account_id}\t{folder_text}\t"
            f"{item.sender}\t{item.subject}\t{size_text}"
        )
    print(f"next_cursor: {page.next_cursor.to_string() if page.next_cursor else ''}")


def _run_search_command(
    args: argparse.Namespace,
    repo: SqliteMessageRepository,
    search_repo: SqliteSearchRepository,
) -> int:
    account_ids = tuple(args.accounts) if args.accounts is not None else None
    filters = MessageFilter(
        account_ids=account_ids,
        folder_ids=_search_folder_ids(repo, account_ids, args.folders),
        date_from=_date_start(args.since),
        date_to=_date_end(args.until),
        has_attachment=args.has_attachment,
    )
    if (
        filters.date_from is not None
        and filters.date_to is not None
        and filters.date_from > filters.date_to
    ):
        raise ConfigError("--since must not be later than --until")
    cursor = None
    if args.after is not None:
        try:
            cursor = PageCursor.from_string(args.after)
        except ValueError as error:
            raise ConfigError("--after is not a valid page cursor") from error

    plan = parse_query(args.query, mode=args.mode)
    if plan.has_slow_path:
        print(
            "短い語を含むため時間がかかる場合があります",
            file=sys.stderr,
        )
    token = CancelToken()
    previous_handler = _install_cancel_handler(token)
    try:
        page = search_messages(
            search_repo,
            query=args.query,
            mode=args.mode,
            filters=filters,
            cursor=cursor,
            limit=args.limit,
            cancel=token,
        )
    finally:
        _restore_cancel_handler(previous_handler)
    _print_search_page(page, as_json=args.json)
    return 0


def _run_account_command(
    args: argparse.Namespace,
    repo: SqliteMessageRepository,
    credential_store: BaseCredentialStore,
    storage_root: Path,
) -> None:
    account_command = getattr(args, "account_command", None)
    if account_command == "add":
        password = getpass.getpass("IMAP password: ")
        account_id = args.account_id
        with ManifestWriter(storage_root, account_id) as manifest:
            account_id = register_account(
                repo,
                credential_store,
                account_id=account_id,
                host=args.host,
                port=args.port,
                username=args.username,
                password=password,
                display_name=getattr(args, "display_name", None),
                manifest=manifest,
                manifest_reader=ManifestReader(storage_root, account_id),
            )
        print(f"Registered account: {account_id}")
        return
    if account_command == "list":
        _print_accounts(list_accounts(repo))
        return
    raise ConfigError(f"Unknown account command: {account_command}")


def _run_folders_command(
    args: argparse.Namespace,
    repo: SqliteMessageRepository,
    credential_store: BaseCredentialStore,
    settings: config.AppConfig,
    storage_root: Path,
) -> None:
    account_id = args.account
    if args.refresh:
        account = _account_by_id(repo, account_id)
        with (
            _account_fetcher(account, credential_store, settings) as fetcher,
            ManifestWriter(storage_root, account_id) as manifest,
        ):
            result = refresh_folders(
                fetcher,
                repo,
                account_id,
                manifest=manifest,
                manifest_reader=ManifestReader(storage_root, account_id),
            )
        print(f"Discovered {result.new_count} new folder(s).")
        for raw_name in result.removed_raw_names:
            print(f"Remote folder unavailable: {raw_name}", file=sys.stderr)
    if args.enable is not None:
        with ManifestWriter(storage_root, account_id) as manifest:
            set_sync_target(
                repo,
                account_id,
                args.enable,
                True,
                manifest=manifest,
                manifest_reader=ManifestReader(storage_root, account_id),
            )
    elif args.disable is not None:
        with ManifestWriter(storage_root, account_id) as manifest:
            set_sync_target(
                repo,
                account_id,
                args.disable,
                False,
                manifest=manifest,
                manifest_reader=ManifestReader(storage_root, account_id),
            )
    _print_folders(repo.list_folders(account_id))


def _run_sync_command(
    args: argparse.Namespace,
    repo: SqliteMessageRepository,
    credential_store: BaseCredentialStore,
    storage_root: Path,
    settings: config.AppConfig,
) -> int:
    accounts = list(repo.list_accounts())
    if args.account is not None:
        accounts = [_account_by_id(repo, args.account)]
    if not accounts:
        raise DatabaseError("No accounts are registered")

    token = CancelToken()
    previous_handler = _install_cancel_handler(token)
    try:
        for account in accounts:
            account_id = _account_id(account)
            print(f"Synchronizing account: {account_id}")
            fetcher = _account_fetcher(account, credential_store, settings)
            storage = EmlStorage(storage_root)
            with ManifestWriter(storage_root, account_id) as manifest, fetcher:
                result = sync_account(
                    fetcher,
                    repo,
                    storage,
                    manifest,
                    account_id=account_id,
                    options=SyncOptions(
                        max_message_bytes=settings.max_message_bytes,
                        flag_refresh_enabled=settings.flag_refresh_enabled,
                        flag_refresh_window_days=settings.flag_refresh_window_days,
                        flag_refresh_min_interval_seconds=settings.flag_refresh_min_interval_seconds,
                    ),
                    cancel=token,
                    on_progress=_print_sync_progress,
                )
            print(
                f"{account_id}: fetched={result.fetched_count}, "
                f"bytes={_format_bytes(result.transferred_bytes)}, "
                f"skipped={result.skipped_count}, failed={result.failed_count}"
            )
            if result.cancelled:
                return 130
        return 0
    finally:
        _restore_cancel_handler(previous_handler)


def _run_reparse_command(
    args: argparse.Namespace,
    repo: SqliteMessageRepository,
    storage_root: Path,
) -> int:
    token = CancelToken()
    previous_handler = _install_cancel_handler(token)
    try:
        result = reparse_messages(
            repo,
            EmlStorage(storage_root),
            account_id=args.account,
            only_failed=not args.all,
            cancel=token,
        )
    finally:
        _restore_cancel_handler(previous_handler)
    print(
        f"Reparsed={result.reparsed_count}, skipped={result.skipped_count}, "
        f"missing={result.missing_count}, hash_mismatch={result.hash_mismatch_count}, "
        f"parse_failed={result.parse_failed_count}"
    )
    return 130 if result.cancelled else 0


class _AccountIntegrityStorage(BaseIntegrityStorage):
    """Limit EML enumeration to one account for account-scoped CLI checks."""

    def __init__(self, storage: BaseIntegrityStorage, account_id: str) -> None:
        self._storage = storage
        self._account_id = account_id

    def stat(self, relative_path: str) -> os.stat_result:
        return self._storage.stat(relative_path)

    def iter_chunks(self, relative_path: str, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        yield from self._storage.iter_chunks(relative_path, chunk_size)

    def iter_eml_paths(self, account_id: str | None = None) -> Iterator[str]:
        del account_id
        yield from self._storage.iter_eml_paths(self._account_id)

    def quarantine(self, relative_path: str) -> None:
        self._storage.quarantine(relative_path)


def _manifest_account_ids(
    storage_root: Path,
    repo: SqliteMessageRepository,
    requested_account_id: str | None = None,
) -> tuple[str, ...]:
    if requested_account_id is not None:
        return (requested_account_id,)
    manifest_root = storage_root / "manifests" / "imap"
    account_ids = (
        tuple(sorted(path.name for path in manifest_root.iterdir() if path.is_dir()))
        if manifest_root.is_dir()
        else ()
    )
    if account_ids:
        return account_ids
    return tuple(
        account_id
        for account in repo.list_accounts()
        if (account_id := account.get("id")) is not None and isinstance(account_id, str)
    )


def _print_verify_progress(progress: VerifyProgress) -> None:
    print(f"checked={progress.checked_count}/{progress.total_count}: {progress.current_path}")


def _print_reindex_progress(progress: ReindexProgress) -> None:
    print(f"reindex={progress.processed_count}/{progress.total_count}: {progress.relative_path}")


def _print_verify_result(mode: str, result: object) -> None:
    if mode == "quick" and isinstance(result, QuickVerifyResult):
        print(
            f"checked={result.checked_count}, missing={result.missing_count}, "
            f"size_mismatch={result.size_mismatch_count}"
        )
    elif mode == "range" and isinstance(result, RangeVerifyResult):
        print(
            f"checked={result.checked_count}, issues={len(result.issues)}, "
            f"repaired={result.repaired_count}, quarantined={result.quarantined_count}"
        )
        for issue in result.issues:
            print(f"{issue.reason}: {issue.relative_path}", file=sys.stderr)
    elif mode == "full" and isinstance(result, FullVerifyResult):
        print(f"checked={result.checked_count}, issues={len(result.issues)}")
        for issue in result.issues:
            print(f"{issue.reason}: {issue.relative_path}", file=sys.stderr)
    elif mode == "orphans" and isinstance(result, OrphanScanResult):
        print(
            f"checked={result.checked_count}, registerable={len(result.registerable)}, "
            f"quarantined={len(result.quarantined_paths)}"
        )
        for path in result.quarantined_paths:
            print(f"quarantined: {path}", file=sys.stderr)
    elif mode == "manifest" and isinstance(result, ManifestVerifyResult):
        print(
            f"files={result.files_checked}, records={result.records_checked}, "
            f"repaired_bytes={result.repaired_bytes}"
        )


def _combine_range_results(results: Sequence[RangeVerifyResult]) -> RangeVerifyResult:
    return RangeVerifyResult(
        sum(result.checked_count for result in results),
        tuple(issue for result in results for issue in result.issues),
        sum(result.repaired_count for result in results),
        sum(result.quarantined_count for result in results),
        any(result.cancelled for result in results),
    )


def _combine_orphan_results(results: Sequence[OrphanScanResult]) -> OrphanScanResult:
    return OrphanScanResult(
        sum(result.checked_count for result in results),
        tuple(candidate for result in results for candidate in result.registerable),
        tuple(path for result in results for path in result.quarantined_paths),
        any(result.cancelled for result in results),
    )


def _run_verify_command(
    args: argparse.Namespace,
    repo: SqliteMessageRepository,
    storage_root: Path,
) -> int:
    mode = args.mode
    account_id = args.account
    if account_id is not None and mode not in {"full", "orphans"}:
        raise ConfigError("--account is supported only for full and orphans verification")

    token = CancelToken()
    previous_handler = _install_cancel_handler(token)
    try:
        storage = EmlStorage(storage_root)
        if mode == "quick":
            result: object = quick_verify(repo, storage, cancel=token)
        elif mode == "range":
            range_results = [
                range_verify(
                    repo,
                    storage,
                    ManifestReader(storage_root, current_account_id),
                    cancel=token,
                )
                for current_account_id in _manifest_account_ids(storage_root, repo)
            ]
            result = _combine_range_results(range_results)
        elif mode == "full":
            scoped_storage: BaseIntegrityStorage = (
                _AccountIntegrityStorage(storage, account_id) if account_id is not None else storage
            )
            result = full_verify(
                repo,
                scoped_storage,
                cancel=token,
                on_progress=_print_verify_progress,
            )
        elif mode == "orphans":
            account_ids = _manifest_account_ids(storage_root, repo, account_id)
            orphan_results = [
                orphan_scan(
                    repo,
                    _AccountIntegrityStorage(storage, current_account_id),
                    cancel=token,
                    on_progress=_print_verify_progress,
                    manifest_reader=ManifestReader(storage_root, current_account_id),
                )
                for current_account_id in account_ids
            ]
            if not orphan_results:
                orphan_results = [
                    orphan_scan(
                        repo,
                        storage,
                        cancel=token,
                        on_progress=_print_verify_progress,
                    )
                ]
            result = _combine_orphan_results(orphan_results)
        elif mode == "manifest":
            result = verify_manifest(storage_root, cancel=token)
        else:
            raise ConfigError(f"Unknown verification mode: {mode}")
    finally:
        _restore_cancel_handler(previous_handler)

    _print_verify_result(mode, result)
    return 130 if getattr(result, "cancelled", False) else 0


def _confirm_reindex() -> bool:
    print(
        "Reindex rebuilds the metadata database from manifests and stored EML files.",
        file=sys.stderr,
    )
    print("Existing metadata-cache records may be replaced.", file=sys.stderr)
    try:
        answer = input("Type 'reindex' to continue: ")
    except EOFError:
        return False
    if answer.strip().casefold() == "reindex":
        return True
    print("Reindex cancelled.", file=sys.stderr)
    return False


def _run_reindex_command(
    args: argparse.Namespace,
    repo: SqliteMessageRepository,
    storage_root: Path,
) -> int:
    if not _confirm_reindex():
        return 130

    token = CancelToken()
    previous_handler = _install_cancel_handler(token)
    try:
        storage = EmlStorage(storage_root)
        results = [
            reindex(
                repo,
                storage,
                ManifestReader(storage_root, account_id),
                cancel=token,
                on_progress=_print_reindex_progress,
            )
            for account_id in _manifest_account_ids(storage_root, repo, args.account)
        ]
    finally:
        _restore_cancel_handler(previous_handler)

    if not results:
        print("No account manifests found.", file=sys.stderr)
        return 0
    for result in results:
        for warning in result.warnings:
            print(f"warning: {warning}", file=sys.stderr)
    print(
        f"accounts={sum(result.account_count for result in results)}, "
        f"folders={sum(result.folder_count for result in results)}, "
        f"messages={sum(result.message_count for result in results)}, "
        f"contents={sum(result.contents_count for result in results)}, "
        f"purged={sum(result.purged_count for result in results)}, "
        f"skipped={sum(result.skipped_count for result in results)}"
    )
    return 130 if any(result.cancelled for result in results) else 0


def _run_application_command(
    args: argparse.Namespace,
    repo: SqliteMessageRepository,
    search_repo: SqliteSearchRepository,
    storage_root: Path,
    settings: config.AppConfig,
    credential_store: BaseCredentialStore,
) -> int:
    command = getattr(args, "command", None)
    if command == "account":
        _run_account_command(args, repo, credential_store, storage_root)
        return 0
    if command == "folders":
        _run_folders_command(args, repo, credential_store, settings, storage_root)
        return 0
    if command == "sync":
        return _run_sync_command(args, repo, credential_store, storage_root, settings)
    if command == "reparse":
        return _run_reparse_command(args, repo, storage_root)
    if command == "verify":
        return _run_verify_command(args, repo, storage_root)
    if command == "reindex":
        return _run_reindex_command(args, repo, storage_root)
    if command == "search":
        return _run_search_command(args, repo, search_repo)
    return 0


def _run_command(
    settings: config.AppConfig,
    requested_root: Path | None,
    command: str | None,
    args: argparse.Namespace | None = None,
) -> int:
    verify_mode = getattr(args, "mode", "quick") if command == "verify" else None
    readonly = command == "verify" and verify_mode in {"quick", "full"}
    result = 0
    with StorageSession(settings, requested_root, readonly=readonly) as session:
        if command not in {None, "migrate"}:
            if args is None:
                raise ConfigError("Command arguments are missing")
            repository = SqliteMessageRepository(session.connection_manager)
            search_repository = SqliteSearchRepository(session.connection_manager)
            if command not in {"verify", "reindex"}:
                backfill_snapshots(
                    repository,
                    lambda account_id: ManifestWriter(session.root, account_id),
                    lambda account_id: ManifestReader(session.root, account_id),
                )
                if session.was_unclean_shutdown:
                    repair_manifest_tails(
                        repository,
                        lambda account_id: ManifestReader(session.root, account_id),
                    )
                    recover_after_unclean_shutdown(
                        repository,
                        EmlStorage(session.root),
                        EmlStorage(session.root),
                        lambda account_id: ManifestReader(session.root, account_id),
                        lambda account_id: ManifestWriter(session.root, account_id),
                        storage_state=StorageStateMachine(),
                    )
            result = _run_application_command(
                args,
                repository,
                search_repository,
                session.root,
                session.settings,
                session.active_credential_store,
            )
    return result


def _run_gui(settings: config.AppConfig, requested_root: Path | None) -> int:
    """Start the GUI without importing PySide6 on CLI-only code paths."""

    from mail_dock.presentation.app import run_gui

    return run_gui(settings, requested_root=requested_root)


def _exit_code(error: MailDockError) -> int:
    if isinstance(error, StorageLockedError | StorageUnsupportedError):
        return 3
    if isinstance(error, DatabaseError):
        return 4
    if isinstance(error, AuthenticationError):
        return 5
    if isinstance(error, FetchError):
        return 6
    if isinstance(error, SearchQueryError):
        return 7
    if isinstance(error, OperationCancelledError):
        return 130
    if isinstance(error, ConfigError | StorageError):
        return 2
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    """Run the mail-dock command-line or graphical application."""

    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        settings = config.load()
        setup_logging(config.config_dir(), debug=bool(getattr(args, "debug", False)))
        command = getattr(args, "command", None)
        if command in {None, "gui"}:
            return _run_gui(settings, getattr(args, "storage_root", None))
        return _run_command(
            settings,
            getattr(args, "storage_root", None),
            command,
            args,
        )
    except KeyboardInterrupt:
        return 130
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
