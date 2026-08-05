"""GUI bootstrap and lifetime management."""

from __future__ import annotations

import logging
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from PySide6.QtCore import QCoreApplication, QObject, QThread, Signal, Slot
from PySide6.QtWidgets import QApplication, QDialog

from mail_dock import config
from mail_dock.__main__ import (
    StorageSession,
    _exit_code,
    _verify_database,
    _verify_fts_database,
)
from mail_dock.domain.errors import MailDockError, StorageForeignRootError
from mail_dock.infrastructure.storage.storage_root import RootProbe, resolve_root
from mail_dock.presentation import strings
from mail_dock.presentation.context import AppContext
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
                    journal_mode="DELETE" if self._session.network_drive else "WAL",
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
        root = _available_root(settings, requested_root)
        setup_wizard: SetupWizard | None = None
        if root is None:

            def start_session(selected_root: Path) -> AppContext:
                nonlocal session, context
                session = StorageSession(settings, selected_root)
                session.__enter__()
                context = AppContext(session, settings)
                updated_settings = replace(
                    settings,
                    storage_root_uuid=session.root_uuid,
                    storage_root_candidates=(str(selected_root.resolve(strict=False)),),
                )
                context.save_settings(updated_settings)
                return context

            setup_wizard = SetupWizard(
                initial_root=requested_root,
                expected_root_uuid=settings.storage_root_uuid,
                on_root_confirmed=start_session,
            )
            if setup_wizard.exec() != QDialog.DialogCode.Accepted:
                return 0
            root = setup_wizard.selected_root
            if root is None or session is None or context is None:
                return 1
        else:
            session = StorageSession(settings, root)
            session.__enter__()
            context = AppContext(session, settings)
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
