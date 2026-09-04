"""GUI bootstrap and lifetime management."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from PySide6.QtCore import QCoreApplication, QObject, Qt, QThread, Signal, Slot
from PySide6.QtWidgets import QApplication, QDialog

from mail_dock import config as config
from mail_dock.__main__ import (
    StorageSession,
    _exit_code,
    _verify_database,
    _verify_fts_database,
)
from mail_dock.domain.errors import (
    ConfigError,
    DatabaseError,
    MailDockError,
    StorageForeignRootError,
    StorageUnsupportedError,
)
from mail_dock.domain.ports import BaseIntegrityStorage
from mail_dock.domain.storage_state import StorageState, StorageStateMachine
from mail_dock.infrastructure.database.migrator import current_version
from mail_dock.infrastructure.storage.capabilities import (
    capability_level,
    probe_capabilities,
    storage_fingerprint,
)
from mail_dock.infrastructure.storage.eml_storage import cleanup_tmp
from mail_dock.infrastructure.storage.storage_root import (
    RootProbe,
    check_free_space,
    drive_kind,
    ensure_layout,
    free_space,
    initialize_root,
    probe,
    resolve_root,
)
from mail_dock.presentation import strings
from mail_dock.presentation.context import AppContext
from mail_dock.presentation.native.device_watcher import DeviceWatcher
from mail_dock.presentation.storage_monitor import StorageMonitor
from mail_dock.presentation.views.dialogs.confirmation_dialog import ConfirmationDialog
from mail_dock.presentation.views.dialogs.error_dialog import show_error
from mail_dock.presentation.views.dialogs.progress_dialog import run_with_progress
from mail_dock.presentation.views.setup_wizard import SetupWizard
from mail_dock.presentation.web.schemes import register_schemes
from mail_dock.usecases.snapshots import (
    backfill_snapshots,
    recover_after_unclean_shutdown,
    repair_manifest_tails,
)
from mail_dock.usecases.trash import (
    PurgeResult,
    list_startup_purge_candidates,
    purge,
)

LOGGER = logging.getLogger(__name__)


def _verify_reconnected_storage(session: StorageSession) -> None:
    """Check the reopened database before allowing a recovered session to run."""

    manager = session.connection_manager
    connection = manager.get_connection()
    try:
        version = current_version(connection)
        if version <= 0:
            raise DatabaseError("Reconnected database has no schema version")
        _verify_database(connection)
    finally:
        manager.close_current_thread()


class _StartupVerificationWorker(QObject):
    """Run startup database checks away from the Qt GUI thread."""

    finished = Signal()
    failed = Signal(object)

    def __init__(self, session: StorageSession, mode: str) -> None:
        super().__init__()
        self._session = session
        self._mode = mode

    @Slot()
    def run(self) -> None:
        try:
            connection = self._session.connection_manager.get_connection()
            _verify_database(connection)
            self._session.connection_manager.close_current_thread()
            if self._mode == "full":
                _verify_fts_database(
                    self._session.root / "metadata.db",
                    journal_mode=self._session.journal_mode or "DELETE",
                )
        except BaseException as error:
            self.failed.emit(error)
        else:
            self.finished.emit()


class _StartupVerificationCompletion(QObject):
    """Handle verification results on the GUI thread."""

    def __init__(
        self,
        app: QCoreApplication,
        session: StorageSession,
        context: AppContext,
        result: dict[str, Any],
        window_factory: Callable[[AppContext], Any] | None = None,
    ) -> None:
        super().__init__(app)
        self._app = app
        self._session = session
        self._context = context
        self._result = result
        self._window_factory = window_factory

    @Slot()
    def verified(self) -> None:
        if self._window_factory is not None:
            window = self._window_factory(self._context)
        else:
            window = self._context.build_main_window()
        self._result["window"] = window
        try:
            _run_startup_purge(self._context, window)
        except BaseException as error:
            self._result["error"] = error
            _show_error(error)
            self._app.quit()
            return
        window.show()
        if self._session.settings.sync_on_startup:
            start_sync = getattr(window, "start_startup_sync", None)
            if callable(start_sync):
                start_sync()

    @Slot(object)
    def failed(self, error: object) -> None:
        if isinstance(error, BaseException):
            self._result["error"] = error
            _show_error(error)
        self._app.quit()


def _startup_purge_confirmation_message(candidates: tuple[Mapping[str, Any], ...]) -> str:
    total_size = sum(
        int(record["size_bytes"])
        for record in candidates
        if isinstance(record.get("size_bytes"), int) and record["size_bytes"] >= 0
    )
    subjects = "\n".join(
        f"- {str(record.get('subject') or '(件名なし)')[:80]}" for record in candidates
    )
    return (
        f"猶予期間を過ぎたメール {len(candidates)} 件を完全削除します。\n"
        f"合計サイズ: {total_size} bytes\n\n{subjects}\n\n実行しますか?"
    )


def _run_startup_purge(context: AppContext, parent: Any = None) -> PurgeResult | None:
    """Apply the configured automatic purge policy before startup sync."""

    settings = context.settings
    if settings.purge_mode == "manual":
        return None

    repository = context.create_message_repository()
    candidates = tuple(
        list_startup_purge_candidates(
            repository,
            mode=settings.purge_mode,
            now=datetime.now(UTC),
            grace_days=settings.trash_grace_days,
        )
    )
    if not candidates:
        return PurgeResult()
    if (
        settings.purge_mode == "grace"
        and not ConfirmationDialog(
            _startup_purge_confirmation_message(candidates), parent
        ).confirmed()
    ):
        return PurgeResult(skipped_ids=tuple(record.get("id") for record in candidates))

    storage_state = StorageStateMachine()
    storage = context.create_purge_storage()
    purged_ids: list[Any] = []
    skipped_ids: list[Any] = []
    physical_paths: list[str] = []
    shared_paths: list[str] = []
    total_size_bytes = 0
    records_by_account: dict[str, list[Any]] = {}
    for record in candidates:
        account_id = record.get("account_id")
        message_id = record.get("id")
        if isinstance(account_id, str) and message_id is not None:
            records_by_account.setdefault(account_id, []).append(message_id)
    for account_id, message_ids in records_by_account.items():
        manifest = context.create_manifest_writer(account_id)
        try:
            result = purge(
                repository,
                storage,
                manifest,
                message_ids=message_ids,
                storage_state=storage_state,
            )
        finally:
            manifest.close()
        purged_ids.extend(result.purged_ids)
        skipped_ids.extend(result.skipped_ids)
        physical_paths.extend(result.physically_deleted_paths)
        shared_paths.extend(result.shared_paths)
        total_size_bytes += result.total_size_bytes
    return PurgeResult(
        purged_ids=tuple(purged_ids),
        skipped_ids=tuple(skipped_ids),
        physically_deleted_paths=tuple(physical_paths),
        shared_paths=tuple(shared_paths),
        total_size_bytes=total_size_bytes,
    )


def _available_root(settings: config.AppConfig, requested_root: Path | None) -> Path | None:
    """Resolve a known root without creating a marker or acquiring a lock."""

    candidates = (
        (requested_root,)
        if requested_root is not None
        else tuple(Path(candidate) for candidate in settings.storage_root_candidates)
    )
    resolution = resolve_root(candidates, settings.storage_root_uuid)
    if resolution.probe is RootProbe.FOREIGN:
        raise StorageForeignRootError(strings.ERROR_FOREIGN_ROOT)
    return resolution.path


def _rebase_root_candidates(
    candidates: tuple[str, ...],
    current_root: Path,
    drives: object,
) -> tuple[Path, ...]:
    """Rebase Windows root candidates onto drives reported as newly arrived."""

    if not isinstance(drives, (tuple, list, set, frozenset)):
        return ()
    arrived_drives = tuple(
        str(drive).rstrip("\\/")
        for drive in drives
        if len(str(drive).rstrip("\\/")) == 2 and str(drive).rstrip("\\/")[1] == ":"
    )
    if not arrived_drives:
        return ()

    rebased: list[Path] = []
    seen: set[str] = set()
    for source in (*map(Path, candidates), current_root):
        drive, tail = os.path.splitdrive(str(source))
        if not drive:
            continue
        for arrived_drive in arrived_drives:
            candidate = Path(arrived_drive + tail)
            key = os.path.normcase(str(candidate))
            if key not in seen:
                seen.add(key)
                rebased.append(candidate)
    return tuple(rebased)


def _show_error(error: BaseException) -> None:
    show_error(error)


def _confirm_storage_unsupported(error: StorageUnsupportedError) -> bool:
    del error
    return ConfirmationDialog(strings.DIALOG_CONFIRM_STORAGE_UNSUPPORTED).confirmed()


def _acknowledge_storage_unsupported(
    settings: config.AppConfig,
    error: StorageUnsupportedError,
) -> config.AppConfig:
    profiles = dict(settings.storage_profiles)
    raw_profile = profiles.get(error.root_uuid)
    if not isinstance(raw_profile, dict):
        raise ConfigError("Storage capability result is missing from configuration")
    profile = dict(raw_profile)
    profile["capability_ack_at"] = datetime.now(UTC).isoformat()
    profiles[error.root_uuid] = profile
    acknowledged = replace(settings, storage_profiles=profiles)
    config.save(acknowledged)
    return config.load()


def _probe_setup_root(
    settings: config.AppConfig,
    root: Path,
    encryption: str,
) -> tuple[config.AppConfig, dict[str, object]]:
    """Probe a newly selected root without initializing its archive layout."""

    checked_path = _normalized_storage_path(root)
    fingerprint = storage_fingerprint(root)
    capabilities = probe_capabilities(root)
    level = capability_level(capabilities)
    return settings, {
        "capabilities": capabilities.as_dict(),
        "capability_level": level.value,
        "checked_path": checked_path,
        "storage_fingerprint": fingerprint,
        "encryption": encryption,
    }


def _commit_setup_root(
    settings: config.AppConfig,
    root: Path,
    result: Mapping[str, object],
) -> config.AppConfig:
    """Initialize a confirmed root and persist its completed probe result."""

    marker = initialize_root(root)
    ensure_layout(root)
    cleanup_tmp(root)
    capabilities = result.get("capabilities")
    capability_level_value = result.get("capability_level")
    checked_path = result.get("checked_path")
    fingerprint = result.get("storage_fingerprint")
    encryption = result.get("encryption")
    if not isinstance(capabilities, dict):
        raise ConfigError("Storage capability result is invalid")
    if not isinstance(capability_level_value, str):
        raise ConfigError("Storage capability result is invalid")
    if not isinstance(checked_path, str):
        raise ConfigError("Storage capability result is invalid")
    if not isinstance(fingerprint, str):
        raise ConfigError("Storage capability result is invalid")
    if not isinstance(encryption, str):
        raise ConfigError("Storage capability result is invalid")
    capabilities_value = cast(config.JSONValue, capabilities)
    profile_updates: dict[str, config.JSONValue] = {
        "capabilities": capabilities_value,
        "capability_level": capability_level_value,
        "checked_path": checked_path,
        "storage_fingerprint": fingerprint,
        "encryption": encryption,
        "encryption_declared_at": datetime.now(UTC).isoformat(),
    }
    profiles = dict(settings.storage_profiles)
    raw_profile = profiles.get(marker.root_uuid)
    profile = dict(raw_profile) if isinstance(raw_profile, dict) else {}
    profile.update(profile_updates)
    updated = replace(
        settings,
        storage_root_uuid=marker.root_uuid,
        storage_root_candidates=(_normalized_storage_path(root),),
        storage_profiles={**profiles, marker.root_uuid: profile},
    )
    config.save(updated)
    return config.load()


def _normalized_storage_path(root: Path) -> str:
    return os.path.normcase(str(root.expanduser().resolve(strict=False)))


def _stop_window(window: Any, context: AppContext | None) -> None:
    stop_workers = getattr(window, "stop_workers", None)
    if callable(stop_workers):
        stop_workers()
    elif context is not None:
        context.stop_workers()


def _start_verification(
    app: QCoreApplication,
    session: StorageSession,
    context: AppContext,
    *,
    window_factory: Callable[[AppContext], Any] | None = None,
) -> tuple[QThread, dict[str, Any]]:
    thread = QThread()
    worker = _StartupVerificationWorker(session, session.settings.startup_verification)
    worker.moveToThread(thread)
    result: dict[str, Any] = {"error": None, "window": None, "worker": worker}
    completion = _StartupVerificationCompletion(
        app,
        session,
        context,
        result,
        window_factory,
    )
    result["completion"] = completion

    thread.started.connect(worker.run)
    worker.finished.connect(completion.verified, Qt.ConnectionType.QueuedConnection)
    worker.failed.connect(completion.failed, Qt.ConnectionType.QueuedConnection)
    worker.finished.connect(thread.quit)
    worker.failed.connect(thread.quit)
    worker.finished.connect(worker.deleteLater)
    worker.failed.connect(worker.deleteLater)
    thread.start()
    return thread, result


QWizardAccepted = 1


def _start_session(
    settings: config.AppConfig,
    root: Path,
) -> tuple[StorageSession, AppContext]:
    """Start one storage session and compose its presentation context."""

    session = StorageSession(settings, root)
    try:
        session.__enter__()
        context = AppContext(session, session.settings)
        backfill_snapshots(
            context.create_message_repository(),
            context.create_manifest_writer,
            context.create_manifest_reader,
        )
        if getattr(session, "was_unclean_shutdown", False):
            repair_manifest_tails(
                context.create_message_repository(),
                context.create_manifest_reader,
            )
            session.recovery_results = recover_after_unclean_shutdown(
                context.create_message_repository(),
                cast(BaseIntegrityStorage, context.create_eml_storage()),
                context.create_purge_storage(),
                context.create_manifest_reader,
                context.create_manifest_writer,
                storage_state=StorageStateMachine(),
            )
        return session, context
    except BaseException as error:
        session.__exit__(type(error), error, error.__traceback__)
        raise


class _GuiRuntime:
    """Own the replaceable GUI window and exactly one active storage session."""

    def __init__(self, application: QCoreApplication, settings: config.AppConfig) -> None:
        self.application = application
        self.settings = settings
        self.session: StorageSession | None = None
        self.context: AppContext | None = None
        self.window: Any = None
        self.storage_monitor: StorageMonitor | None = None
        self.device_watcher: DeviceWatcher | None = None
        self.verification_thread: QThread | None = None
        self.verification_result: dict[str, Any] = {"error": None, "window": None}
        self.fatal_error: BaseException | None = None
        self._replacement_window: Any = None
        self._reconnect_root: Path | None = None
        self._reconnect_prepared = False

    def attach(self, session: StorageSession, context: AppContext) -> None:
        self.session = session
        self.context = context
        self.settings = session.settings
        context.storage_root_switch_handler = self.switch_root
        context.storage_setup_handler = self.start_setup
        context.storage_detach_handler = self.safe_detach
        context.storage_reconnect_handler = self.request_reconnect
        context.window_created_handler = self._window_created

    def start(self, root: Path) -> AppContext:
        session, context = _start_session(self.settings, root)
        self.attach(session, context)
        return context

    def _window_created(self, window: Any) -> None:
        previous_monitor = self.storage_monitor
        if previous_monitor is not None:
            previous_monitor.stop()
        if self.device_watcher is not None:
            self.device_watcher.uninstall(self.application)
        self.window = window
        if self.session is None or self.context is None:
            return
        workers = tuple(
            worker
            for worker in (
                getattr(window, "query_worker", None),
                getattr(window, "sync_worker", None),
                getattr(window, "verify_worker", None),
            )
            if worker is not None
        )
        self.storage_monitor = StorageMonitor(
            self.session.root,
            self.session.root_uuid,
            self.session.settings,
            storage_lock=getattr(self.session, "storage_lock", None),
            connection_manager=self.session.connection_manager,
            workers=workers,
            reconnect=self._reconnect,
            config_log_dir=config.config_dir(),
            parent=window if isinstance(window, QObject) else None,
        )
        set_storage_state = getattr(window, "set_storage_state", None)
        if callable(set_storage_state):
            self.storage_monitor.storage_state_changed.connect(set_storage_state)
        show_storage_detached = getattr(window, "_show_storage_detached", None)
        if callable(show_storage_detached):
            self.storage_monitor.storage_detached.connect(show_storage_detached)
        self.device_watcher = DeviceWatcher()
        self.device_watcher.device_query_remove.connect(self._handle_device_query_remove)
        self.device_watcher.device_removed.connect(self._handle_device_removed)
        self.device_watcher.device_arrived.connect(self._handle_device_arrived)
        self.device_watcher.install(self.application)
        replacement_window = self._replacement_window
        self._replacement_window = None
        if replacement_window is not None and replacement_window is not window:
            set_attribute = getattr(replacement_window, "setAttribute", None)
            if callable(set_attribute):
                set_attribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
            close = getattr(replacement_window, "close", None)
            if callable(close):
                close()
        self.storage_monitor.start()

    def request_reconnect(self) -> None:
        """Start recovery after the user selects the retry action."""

        monitor = self.storage_monitor
        if monitor is not None:
            monitor.request_reconnect()

    def _device_matches_root(self, drives: object) -> bool:
        if self.session is None:
            return False
        drive, _tail = os.path.splitdrive(str(self.session.root))
        if not drive:
            return True
        if not isinstance(drives, (tuple, list, set, frozenset)):
            return False
        return drive.rstrip("\\/").casefold() in {
            str(candidate).rstrip("\\/").casefold() for candidate in drives
        }

    def _handle_device_query_remove(self, drives: object) -> None:
        if self._device_matches_root(drives):
            self.safe_detach()

    def _handle_device_removed(self, drives: object) -> None:
        if self._device_matches_root(drives) and self.storage_monitor is not None:
            self.storage_monitor.handle_device_removed()

    def _handle_device_arrived(self, drives: object) -> None:
        monitor = self.storage_monitor
        if monitor is None:
            return
        if monitor.state is StorageState.DETACHED:
            reconnected_root = self._root_for_arrived_device(drives)
            if reconnected_root is None:
                return
            monitor.root = reconnected_root
            monitor.handle_device_arrived()
        elif self._device_matches_root(drives):
            monitor.handle_device_arrived()

    def _root_for_arrived_device(self, drives: object) -> Path | None:
        session = self.session
        if session is None or session.root_uuid is None:
            return None
        candidates = _rebase_root_candidates(
            self.settings.storage_root_candidates,
            session.root,
            drives,
        )
        if not candidates:
            return None
        resolution = resolve_root(candidates, session.root_uuid)
        if resolution.probe is RootProbe.FOREIGN:
            return None
        return resolution.path or candidates[0]

    def _prepare_reconnect(self) -> bool:
        if self._reconnect_prepared:
            return self._reconnect_root is not None
        session = self.session
        if session is None:
            return False
        self._reconnect_root = (
            self.storage_monitor.root if self.storage_monitor is not None else session.root
        )
        self._replacement_window = self.window
        if self.storage_monitor is not None:
            self.storage_monitor.stop()
        if self.device_watcher is not None:
            self.device_watcher.uninstall(self.application)
            self.device_watcher = None
        if self.window is not None:
            _stop_window(self.window, self.context)
            hide = getattr(self.window, "hide", None)
            if callable(hide):
                hide()
        session.__exit__(RuntimeError, RuntimeError("storage reconnect"), None)
        self.session = None
        self.context = None
        self._reconnect_prepared = True
        return True

    def _reconnect(self) -> bool:
        """Release old handles and stage a new session for verification."""

        if not self._prepare_reconnect() or self._reconnect_root is None:
            return False
        reconnect_root = self._reconnect_root
        settings = self.settings

        def _run() -> tuple[StorageSession, AppContext]:
            session, context = _start_session(settings, reconnect_root)
            try:
                recovery_results = getattr(session, "recovery_results", ())
                if any(
                    getattr(result, "cancelled", False) or getattr(result, "issues", ())
                    for result in recovery_results
                ):
                    raise DatabaseError("Storage range verification failed after reconnect")
                _verify_reconnected_storage(session)
                if session.root_uuid is None:
                    raise DatabaseError("Reconnected storage root has no UUID")
            except BaseException as error:
                session.__exit__(type(error), error, error.__traceback__)
                raise
            connection_manager = getattr(session, "connection_manager", None)
            close_current_thread = getattr(connection_manager, "close_current_thread", None)
            if callable(close_current_thread):
                close_current_thread()
            return session, context

        try:
            new_session, new_context = cast(
                "tuple[StorageSession, AppContext]",
                run_with_progress(_run, strings.STATUS_STORAGE_RECONNECTING),
            )
        except BaseException:
            old_window = self._replacement_window
            if old_window is not None:
                show = getattr(old_window, "show", None)
                if callable(show):
                    show()
            return False

        updated_settings = replace(
            self.settings,
            storage_root_candidates=(_normalized_storage_path(new_session.root),),
            storage_root_uuid=new_session.root_uuid,
        )
        new_context.save_settings(updated_settings)
        self.settings = updated_settings
        self.window = None
        self.attach(new_session, new_context)
        self.verify_and_show()
        self._reconnect_root = None
        self._reconnect_prepared = False
        return True

    def verify_and_show(self) -> None:
        if self.session is None or self.context is None:
            raise RuntimeError("GUI session has not started")
        self.verification_thread, self.verification_result = _start_verification(
            self.application,
            self.session,
            self.context,
            window_factory=lambda context: context.build_main_window(),
        )

    def _release_current(self) -> None:
        if self.verification_thread is not None and self.verification_thread.isRunning():
            self.verification_thread.quit()
            self.verification_thread.wait()
        if self.storage_monitor is not None:
            self.storage_monitor.stop()
            self.storage_monitor = None
        if self.device_watcher is not None:
            self.device_watcher.uninstall(self.application)
            self.device_watcher = None
        if self.window is not None:
            _stop_window(self.window, self.context)
            # Prevent Qt's quitOnLastWindowClosed from ending app.exec() before
            # verify_and_show() shows the replacement window (root switch/setup).
            set_attribute = getattr(self.window, "setAttribute", None)
            if callable(set_attribute):
                set_attribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
            # close_for_session_swap() (when available) skips MainWindow.closeEvent's
            # QApplication.quit(), so a run_with_progress() call right after this isn't
            # handed an already-quitting event loop. Fall back to close() for fakes.
            close = getattr(self.window, "close_for_session_swap", None)
            if not callable(close):
                close = getattr(self.window, "close", None)
            if callable(close):
                close()
        elif self.context is not None:
            self.context.stop_workers()
        if self.session is not None:
            self.session.__exit__(None, None, None)
        replacement_window = self._replacement_window
        self._replacement_window = None
        if replacement_window is not None:
            close = getattr(replacement_window, "close", None)
            if callable(close):
                close()
        self.window = None
        self.context = None
        self.session = None
        self.verification_thread = None

    def safe_detach(self) -> None:
        """Release the active root in the order required for safe removal."""

        session = self.session
        window = self.window
        if session is None or window is None:
            return
        try:
            stop_workers = getattr(window, "stop_workers", None)
            if callable(stop_workers):
                stop_workers()
            if self.storage_monitor is not None:
                self.storage_monitor.stop()
            if self.device_watcher is not None:
                self.device_watcher.uninstall(self.application)
                self.device_watcher = None
            checkpoint = getattr(session, "checkpoint_for_detach", None)
            if callable(checkpoint):
                checkpoint()
            detail_view = getattr(window, "detail_view", None)
            close_detail = getattr(detail_view, "close", None)
            if callable(close_detail):
                close_detail()
            session.__exit__(None, None, None)
        except BaseException as error:
            LOGGER.exception("Safe storage detach failed")
            _show_error(error)
            return

        if self.storage_monitor is not None:
            self.storage_monitor.mark_detached_by_user()
        self.session = None
        self.context = None

    def switch_root(self, root: Path) -> None:
        result = probe(root, None)
        if result is RootProbe.MISSING:
            if ConfirmationDialog(strings.DIALOG_CONFIRM_SETUP_MISSING_ROOT).confirmed():
                self.start_setup(root)
            return
        if result is RootProbe.FOREIGN:
            _show_error(StorageForeignRootError(strings.ERROR_FOREIGN_ROOT))
            return
        self._replace_with_root(root)

    def _replace_with_root(self, root: Path) -> None:
        marker = initialize_root(root)
        self._release_current()
        self.settings = replace(
            self.settings,
            storage_root_uuid=marker.root_uuid,
            storage_root_candidates=(_normalized_storage_path(root),),
        )
        settings = self.settings

        def _run() -> tuple[StorageSession, AppContext]:
            session, context = _start_session(settings, root)
            connection_manager = getattr(session, "connection_manager", None)
            close_current_thread = getattr(connection_manager, "close_current_thread", None)
            if callable(close_current_thread):
                close_current_thread()
            return session, context

        result = run_with_progress(_run, strings.STATUS_STORAGE_SWITCHING)
        if not isinstance(result, tuple) or len(result) != 2:
            raise DatabaseError("Storage root switch produced no session")
        session, context = cast("tuple[StorageSession, AppContext]", result)
        self.attach(session, context)
        updated = replace(
            context.settings,
            storage_root_uuid=context.root_uuid,
            storage_root_candidates=(_normalized_storage_path(root),),
        )
        context.save_settings(updated)
        self.verify_and_show()

    def start_setup(self, initial_root: Path | None = None) -> None:
        old_root = self.context.storage_root if self.context is not None else None
        old_settings = self.settings
        pending_probe: dict[str, object] | None = None

        def probe_root(selected_root: Path, encryption: str) -> Mapping[str, object]:
            nonlocal pending_probe
            _unchanged_settings, result = _probe_setup_root(
                self.settings,
                selected_root,
                encryption,
            )
            pending_probe = result
            return result

        def before_confirm(selected_root: Path) -> None:
            if pending_probe is None:
                raise ConfigError("Storage root must be probed before confirmation")
            # Must run on the GUI thread: releases the QTimer/QWidget-owning old session.
            self._release_current()

        def start_session(selected_root: Path) -> AppContext:
            if pending_probe is None:
                raise ConfigError("Storage root must be probed before confirmation")
            self.settings = _commit_setup_root(self.settings, selected_root, pending_probe)
            context = self.start(selected_root)
            # Runs on run_with_progress()'s worker thread: leaving this connection
            # owned by that thread makes assert_all_closed() fail on next release.
            connection_manager = getattr(self.session, "connection_manager", None)
            close_current_thread = getattr(connection_manager, "close_current_thread", None)
            if callable(close_current_thread):
                close_current_thread()
            return context

        wizard = SetupWizard(
            initial_root=initial_root,
            context=None,
            expected_root_uuid=self.settings.storage_root_uuid,
            on_root_confirmed=start_session,
            on_before_confirm=before_confirm,
            on_root_probe=probe_root,
            on_root_identity_probe=lambda path: probe(path, self.settings.storage_root_uuid).value,
            check_root_space=lambda path: check_free_space(path).value,
            resolve_drive_kind=lambda path: drive_kind(path).value,
            resolve_free_space=free_space,
        )
        accepted = wizard.exec() == QDialog.DialogCode.Accepted
        if accepted and self.session is not None:
            self.verify_and_show()
        elif not accepted and old_root is not None and self.session is not None:
            current_root = self.context.storage_root if self.context is not None else None
            if current_root != old_root:
                self._release_current()
                self.settings = old_settings
                config.save(old_settings)
                try:
                    self.start(old_root)
                except BaseException as error:
                    # Nothing left to fall back to: report it and let run_gui() unwind.
                    LOGGER.exception("Failed to restore the previous storage root")
                    self.fatal_error = error
                    _show_error(error)
                    self.application.quit()
                    return
                self.verify_and_show()

    def close(self) -> None:
        self._release_current()


def run_gui(settings: config.AppConfig, *, requested_root: Path | None = None) -> int:
    """Run the GUI and return its process exit code.

    Scheme registration intentionally precedes QApplication construction. A
    storage session is created only after root selection has completed, which
    keeps first-run bootstrap usable without a configured or attached drive.
    """

    register_schemes()
    app = QApplication.instance() or QApplication(sys.argv)
    session: StorageSession | None = None
    context: AppContext | None = None
    window: Any = None
    verification_thread: QThread | None = None
    verification_result: dict[str, Any] = {"error": None, "window": None}
    session_error: BaseException | None = None
    runtime: _GuiRuntime | None = None
    try:
        active_settings = settings
        root = _available_root(active_settings, requested_root)
        setup_wizard: SetupWizard | None = None
        if root is None:
            pending_probe: dict[str, object] | None = None

            def probe_root(selected_root: Path, encryption: str) -> Mapping[str, object]:
                nonlocal pending_probe
                _unchanged_settings, result = _probe_setup_root(
                    active_settings,
                    selected_root,
                    encryption,
                )
                pending_probe = result
                return result

            def start_session(selected_root: Path) -> AppContext:
                nonlocal active_settings, session, context
                if pending_probe is None:
                    raise ConfigError("Storage root must be probed before confirmation")
                active_settings = _commit_setup_root(
                    active_settings,
                    selected_root,
                    pending_probe,
                )
                session, context = _start_session(active_settings, selected_root)
                # Runs on run_with_progress()'s worker thread: see start_setup() above.
                connection_manager = getattr(session, "connection_manager", None)
                close_current_thread = getattr(connection_manager, "close_current_thread", None)
                if callable(close_current_thread):
                    close_current_thread()
                updated_settings = replace(
                    session.settings,
                    storage_root_uuid=session.root_uuid,
                    storage_root_candidates=(_normalized_storage_path(selected_root),),
                )
                context.save_settings(updated_settings)
                return context

            setup_wizard = SetupWizard(
                initial_root=requested_root,
                expected_root_uuid=active_settings.storage_root_uuid,
                on_root_confirmed=start_session,
                on_root_probe=probe_root,
                on_root_identity_probe=lambda path: (
                    probe(
                        path,
                        active_settings.storage_root_uuid,
                    ).value
                ),
                check_root_space=lambda path: check_free_space(path).value,
                resolve_drive_kind=lambda path: drive_kind(path).value,
                resolve_free_space=free_space,
            )
            if setup_wizard.exec() != QDialog.DialogCode.Accepted:
                return 0
            root = setup_wizard.selected_root
            if root is None or session is None or context is None:
                return 1
        else:
            unsupported_acknowledged = False
            while True:
                try:
                    session, context = _start_session(active_settings, root)
                except StorageUnsupportedError as unsupported_error:
                    if unsupported_acknowledged or not _confirm_storage_unsupported(
                        unsupported_error
                    ):
                        session_error = unsupported_error
                        return _exit_code(unsupported_error)
                    active_settings = _acknowledge_storage_unsupported(
                        active_settings, unsupported_error
                    )
                    unsupported_acknowledged = True
                    continue
                break
        runtime = _GuiRuntime(app, active_settings)
        runtime.attach(session, context)
        verification_thread, verification_result = _start_verification(app, session, context)
        runtime.verification_thread = verification_thread
        runtime.verification_result = verification_result
        event_code = app.exec()
        if runtime.fatal_error is not None:
            # start_setup() already released the window/session; nothing left to reconcile.
            session_error = runtime.fatal_error
            return _exit_code(session_error) if isinstance(session_error, MailDockError) else 1
        window = verification_result["window"]
        runtime.window = window
        error = verification_result["error"]
        if isinstance(error, MailDockError):
            session_error = error
            return _exit_code(error)
        if error is not None:
            session_error = error
            return 1
        return event_code
    except BaseException as error:
        session_error = error
        _show_error(error)
        return _exit_code(error) if isinstance(error, MailDockError) else 1
    finally:
        if runtime is not None:
            runtime.close()
            session = None
            context = None
            window = None
            verification_thread = None
        if verification_thread is not None and verification_thread.isRunning():
            verification_thread.quit()
            verification_thread.wait()
        if window is not None:
            _stop_window(window, context)
        elif context is not None:
            context.stop_workers()
        if session is not None:
            if session_error is None:
                session.__exit__(None, None, None)
            else:
                session.__exit__(type(session_error), session_error, session_error.__traceback__)
