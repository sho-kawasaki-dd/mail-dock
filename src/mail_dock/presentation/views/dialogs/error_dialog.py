"""Reusable safe error dialog for GUI operations."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox, QWidget

from mail_dock import config
from mail_dock.domain.errors import MailDockError
from mail_dock.presentation import strings
from mail_dock.presentation.errors import ErrorPresentation, present_error

LOGGER = logging.getLogger(__name__)


class ErrorDialog(QMessageBox):
    """Display a safe error presentation without exposing exception details."""

    def __init__(self, error: BaseException, parent: QWidget | None = None) -> None:
        self.error = error
        self.presentation: ErrorPresentation = present_error(error)
        super().__init__(parent)
        self.setIcon(QMessageBox.Icon.Critical)
        self.setWindowTitle(strings.ERROR_TITLE)
        self.setText(self.presentation.message)
        if self.presentation.recovery_action is not None:
            self.setInformativeText(self.presentation.recovery_action)
        self._log_button = None
        if self.presentation.show_log_folder:
            self._log_button = self.addButton(
                strings.MAIN_MENU_OPEN_LOG_FOLDER,
                QMessageBox.ButtonRole.HelpRole,
            )
        self.addButton(QMessageBox.StandardButton.Ok)
        self.buttonClicked.connect(self._button_clicked)
        if not isinstance(error, MailDockError):
            LOGGER.exception("GUI operation failed", exc_info=error)

    @property
    def log_button(self) -> object | None:
        """Return the optional log-folder button for tests and callers."""

        return self._log_button

    def _button_clicked(self, button: object) -> None:
        if button != self._log_button:
            return
        log_path = Path(config.config_dir()) / "logs"
        log_path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_path)))


def show_error(error: BaseException, parent: QWidget | None = None) -> int:
    """Show an :class:`ErrorDialog` and return its modal result."""

    return ErrorDialog(error, parent).exec()