"""GUI bootstrap and lifetime management."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QCoreApplication, QObject, QThread, Signal, Slot
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
    MailDockError,
    StorageForeignRootError,
    StorageUnsupportedError,
)
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
    resolve_root,
)
from mail_dock.presentation import strings
from mail_dock.presentation.context import AppContext
from mail_dock.presentation.views.dialogs.confirmation_dialog import ConfirmationDialog
from mail_dock.presentation.views.dialogs.error_dialog import show_error
from mail_dock.presentation.views.setup_wizard import SetupWizard
from mail_dock.presentation.web.schemes import register_schemes

LOGGER = logging.getLogger(__name__)


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
    """Probe a newly selected root and persist its primitive result."""

    marker = initialize_root(root)
    ensure_layout(root)
    cleanup_tmp(root)
    checked_path = _normalized_storage_path(root)
    fingerprint = storage_fingerprint(root)
    capabilities = probe_capabilities(root)
    level = capability_level(capabilities)
    profiles = dict(settings.storage_profiles)
    raw_profile = profiles.get(marker.root_uuid)
    profile = dict(raw_profile) if isinstance(raw_profile, dict) else {}
    profile.update(
        {
            "capabilities": capabilities.as_dict(),
            "capability_level": level.value,
            "checked_path": checked_path,
            "storage_fingerprint": fingerprint,
            "encryption": encryption,
            "encryption_declared_at": datetime.now(UTC).isoformat(),
        }
    )
    profiles[marker.root_uuid] = profile
    updated = replace(
        settings,
        storage_root_uuid=marker.root_uuid,
        storage_root_candidates=(checked_path,),
        storage_profiles=profiles,
    )
    config.save(updated)
    return config.load(), {
        "capabilities": capabilities.as_dict(),
        "capability_level": level.value,
        "checked_path": checked_path,
        "storage_fingerprint": fingerprint,
        "encryption": encryption,
    }


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
) -> tuple[QThread, dict[str, Any]]:
    thread = QThread()
    worker = _StartupVerificationWorker(session, session.settings.startup_verification)
    worker.moveToThread(thread)
    result: dict[str, Any] = {"error": None, "window": None, "worker": worker}

    def verified() -> None:
        window = context.build_main_window()
        result["window"] = window
        window.show()
        if session.settings.sync_on_startup:
            start_sync = getattr(window, "start_startup_sync", None)
            if callable(start_sync):
                start_sync()

    def failed(error: BaseException) -> None:
        result["error"] = error
        _show_error(error)
        app.quit()

    thread.started.connect(worker.run)
    worker.finished.connect(verified)
    worker.failed.connect(failed)
    worker.finished.connect(thread.quit)
    worker.failed.connect(thread.quit)
    worker.finished.connect(worker.deleteLater)
    worker.failed.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    thread.start()
    return thread, result


QWizardAccepted = 1


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
    try:
        active_settings = settings
        root = _available_root(active_settings, requested_root)
        setup_wizard: SetupWizard | None = None
        if root is None:

            def probe_root(selected_root: Path, encryption: str) -> Mapping[str, object]:
                nonlocal active_settings
                active_settings, result = _probe_setup_root(
                    active_settings,
                    selected_root,
                    encryption,
                )
                return result

            def start_session(selected_root: Path) -> AppContext:
                nonlocal session, context
                session = StorageSession(active_settings, selected_root)
                session.__enter__()
                context = AppContext(session, session.settings)
                updated_settings = replace(
                    session.settings,
                    storage_root_uuid=session.root_uuid,
                    storage_root_candidates=(str(selected_root.resolve(strict=False)),),
                )
                context.save_settings(updated_settings)
                return context

            setup_wizard = SetupWizard(
                initial_root=requested_root,
                expected_root_uuid=active_settings.storage_root_uuid,
                on_root_confirmed=start_session,
                on_root_probe=probe_root,
                root_initializer=lambda path: initialize_root(path).root_uuid,
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
                session = StorageSession(active_settings, root)
                try:
                    session.__enter__()
                except StorageUnsupportedError as unsupported_error:
                    if unsupported_acknowledged or not _confirm_storage_unsupported(
                        unsupported_error
                    ):
                        session_error = unsupported_error
                        return _exit_code(unsupported_error)
                    active_settings = _acknowledge_storage_unsupported(
                        session.settings, unsupported_error
                    )
                    unsupported_acknowledged = True
                    continue
                break
            context = AppContext(session, session.settings)
        verification_thread, verification_result = _start_verification(app, session, context)
        event_code = app.exec()
        window = verification_result["window"]
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
