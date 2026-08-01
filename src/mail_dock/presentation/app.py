"""GUI bootstrap and lifetime management."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import QCoreApplication, QObject, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

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
from mail_dock.presentation.errors import present_error
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
                    network_drive=self._session.network_drive,
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


def _show_setup_wizard(requested_root: Path | None) -> Path | None:
    wizard = SetupWizard(initial_root=requested_root)
    if wizard.exec() != QDialog.DialogCode.Accepted:
        return None
    return wizard.selected_root


def _show_error(error: BaseException) -> None:
    presentation = present_error(error)
    if not isinstance(error, MailDockError):
        LOGGER.exception("GUI startup failed", exc_info=error)
    box = QMessageBox(QMessageBox.Icon.Critical, strings.APP_TITLE, presentation.message)
    if presentation.recovery_action is not None:
        box.setInformativeText(presentation.recovery_action)
    log_button = None
    if presentation.show_log_folder:
        log_button = box.addButton(
            strings.MAIN_MENU_OPEN_LOG_FOLDER,
            QMessageBox.ButtonRole.HelpRole,
        )
    box.addButton(QMessageBox.StandardButton.Ok)
    box.exec()
    if box.clickedButton() is log_button:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(config.config_dir() / "logs")))


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
    result: dict[str, Any] = {"error": None, "window": None}

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
        if root is None:
            root = _show_setup_wizard(requested_root)
        if root is None:
            return 0

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
