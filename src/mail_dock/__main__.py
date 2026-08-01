"""The mail-dock composition root and command-line interface."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import signal
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any, cast

from mail_dock import __version__, config
from mail_dock.domain.errors import (
    AuthenticationError,
    ConfigError,
    DatabaseError,
    FetchError,
    MailDockError,
    OperationCancelledError,
    SearchQueryError,
    StorageError,
    StorageForeignRootError,
    StorageLockedError,
    StorageRootMissingError,
)
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.repository import MessageRecord
from mail_dock.domain.search import MessageFilter, MessageSummary, PageCursor, SearchPage
from mail_dock.infrastructure.database.connection import ConnectionManager, connect
from mail_dock.infrastructure.database.fts_maintenance import integrity_check
from mail_dock.infrastructure.database.message_repository import SqliteMessageRepository
from mail_dock.infrastructure.database.migrator import migrate
from mail_dock.infrastructure.database.search_repository import SqliteSearchRepository
from mail_dock.infrastructure.fetchers.onamae_imap import OnamaeImapFetcher
from mail_dock.infrastructure.logging_config import set_storage_log_target, setup_logging
from mail_dock.infrastructure.security.keyring_store import KeyringCredentialStore
from mail_dock.infrastructure.storage.detach import storage_io
from mail_dock.infrastructure.storage.eml_storage import EmlStorage, cleanup_tmp
from mail_dock.infrastructure.storage.manifest import ManifestWriter
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
from mail_dock.usecases.register_account import (
    list_accounts,
    load_credentials,
    register_account,
)
from mail_dock.usecases.reparse import reparse_messages
from mail_dock.usecases.search_messages import search_messages
from mail_dock.usecases.search_query import parse_query
from mail_dock.usecases.sync_folders import refresh_folders, set_sync_target
from mail_dock.usecases.sync_mail import SyncOptions, SyncProgress, sync_account

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


def _verify_fts_database(db_path: Path, *, network_drive: bool) -> None:
    """Run the FTS check on a short-lived writable connection only."""

    connection = connect(db_path, network_drive=network_drive)
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
        self.manager: ConnectionManager | None = None
        self._lock: StorageLock | None = None
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
            check_free_space(root)
            set_storage_log_target(root / "logs")
            self._storage_logging_enabled = True

            self.manager = ConnectionManager(
                root / "metadata.db",
                readonly=self.readonly,
                network_drive=self.network_drive,
            )
            connection = self.manager.get_connection()
            if self.readonly:
                _verify_database(connection)
                self.manager.close_current_thread()
                _verify_fts_database(root / "metadata.db", network_drive=self.network_drive)
                LOGGER.info("Database verification succeeded")
            else:
                version = migrate(connection, root / "metadata.db")
                LOGGER.info("Database migration complete at schema version %d", version)
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
        finally:
            self._cleanup()

    def _save_settings(self) -> None:
        if self._root is None or self.root_uuid is None:
            return
        updated_settings = replace(
            self.settings,
            storage_root_uuid=self.root_uuid,
            storage_root_candidates=(str(self._root.resolve(strict=False)),),
        )
        config.save(updated_settings)

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
    credential_store: KeyringCredentialStore,
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
        load_credentials(credential_store, account_id),
        port=port,
        remote_trash_folder=settings.remote_trash_folder,
    )


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
    credential_store: KeyringCredentialStore,
) -> None:
    account_command = getattr(args, "account_command", None)
    if account_command == "add":
        password = getpass.getpass("IMAP password: ")
        account_id = register_account(
            repo,
            credential_store,
            account_id=args.account_id,
            host=args.host,
            port=args.port,
            username=args.username,
            password=password,
            display_name=getattr(args, "display_name", None),
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
    credential_store: KeyringCredentialStore,
    settings: config.AppConfig,
) -> None:
    account_id = args.account
    if args.refresh:
        account = _account_by_id(repo, account_id)
        with _account_fetcher(account, credential_store, settings) as fetcher:
            result = refresh_folders(fetcher, repo, account_id)
        print(f"Discovered {result.new_count} new folder(s).")
        for raw_name in result.removed_raw_names:
            print(f"Remote folder unavailable: {raw_name}", file=sys.stderr)
    if args.enable is not None:
        set_sync_target(repo, account_id, args.enable, True)
    elif args.disable is not None:
        set_sync_target(repo, account_id, args.disable, False)
    _print_folders(repo.list_folders(account_id))


def _run_sync_command(
    args: argparse.Namespace,
    repo: SqliteMessageRepository,
    credential_store: KeyringCredentialStore,
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
                    options=SyncOptions(max_message_bytes=settings.max_message_bytes),
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


def _run_application_command(
    args: argparse.Namespace,
    repo: SqliteMessageRepository,
    search_repo: SqliteSearchRepository,
    storage_root: Path,
    settings: config.AppConfig,
) -> int:
    credential_store = KeyringCredentialStore()
    command = getattr(args, "command", None)
    if command == "account":
        _run_account_command(args, repo, credential_store)
        return 0
    if command == "folders":
        _run_folders_command(args, repo, credential_store, settings)
        return 0
    if command == "sync":
        return _run_sync_command(args, repo, credential_store, storage_root, settings)
    if command == "reparse":
        return _run_reparse_command(args, repo, storage_root)
    if command == "search":
        return _run_search_command(args, repo, search_repo)
    return 0


def _run_command(
    settings: config.AppConfig,
    requested_root: Path | None,
    command: str | None,
    args: argparse.Namespace | None = None,
) -> int:
    readonly = command == "verify"
    result = 0
    with StorageSession(settings, requested_root, readonly=readonly) as session:
        if command not in {None, "migrate", "verify"}:
            if args is None:
                raise ConfigError("Command arguments are missing")
            repository = SqliteMessageRepository(session.connection_manager)
            search_repository = SqliteSearchRepository(session.connection_manager)
            result = _run_application_command(
                args,
                repository,
                search_repository,
                session.root,
                settings,
            )
    return result


def _run_gui(settings: config.AppConfig, requested_root: Path | None) -> int:
    """Start the GUI without importing PySide6 on CLI-only code paths."""

    from mail_dock.presentation.app import run_gui

    return cast(int, run_gui(settings, requested_root=requested_root))


def _exit_code(error: MailDockError) -> int:
    if isinstance(error, StorageLockedError):
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
