"""Cancellable progress dialog shared by short asynchronous operations."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QProgressDialog, QWidget

from mail_dock.domain.fetcher import CancelToken
from mail_dock.presentation import strings


class ProgressDialog(QProgressDialog):
    """Show an indeterminate operation and cancel its caller-owned token."""

    cancel_requested = Signal()

    def __init__(self, message: str, parent: QWidget | None = None) -> None:
        super().__init__(message, strings.SEARCH_CANCEL, 0, 0, parent)
        self.setWindowTitle(strings.APP_TITLE)
        self.setAutoClose(False)
        self.setAutoReset(False)
        self.setMinimumDuration(0)
        self._token: CancelToken | None = None
        self.canceled.connect(self._cancel_token)

    def attach_token(self, token: CancelToken) -> None:
        """Attach the UI-owned token returned by ``Worker.submit``."""

        self._token = token

    def set_progress(self, value: int, maximum: int) -> None:
        """Switch to determinate progress when an operation can report it."""

        self.setRange(0, max(0, maximum))
        self.setValue(max(0, min(value, maximum)))

    def finish(self) -> None:
        """Close the dialog after a worker result, failure, or cancellation."""

        self._token = None
        self.close()

    def _cancel_token(self) -> None:
        self.cancel_requested.emit()
        if self._token is not None:
            self._token.cancel()
