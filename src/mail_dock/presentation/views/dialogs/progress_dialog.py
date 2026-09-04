"""Cancellable progress dialog shared by short asynchronous operations."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEventLoop, Qt, Signal
from PySide6.QtWidgets import QProgressDialog, QWidget

from mail_dock.domain.errors import MailDockError
from mail_dock.domain.fetcher import CancelToken
from mail_dock.presentation import strings
from mail_dock.presentation.threads.worker import Worker


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


def run_with_progress(
    operation: Callable[[], object],
    message: str,
    *,
    parent: QWidget | None = None,
) -> object:
    """Run a non-cancellable operation on a worker thread while showing progress.

    The call still blocks the caller (so it can gate a synchronous API such as
    ``QWizardPage.validateCurrentPage``), but a nested Qt event loop keeps the
    GUI thread pumping paint and timer events instead of freezing the window.
    """

    dialog = ProgressDialog(message, parent)
    dialog.setCancelButton(None)
    dialog.show()
    worker = Worker(None)
    worker.start()
    loop = QEventLoop()
    outcome: dict[str, object] = {}

    def _succeeded(value: object) -> None:
        outcome["value"] = value
        loop.quit()

    def _failed(error: object) -> None:
        outcome["error"] = error
        loop.quit()

    # Force queued delivery: result/failed are plain closures (not QObject slots), so
    # AutoConnection can resolve to Direct and run on the worker thread, letting
    # loop.quit() race ahead of loop.exec() and get silently dropped by Qt.
    worker.result.connect(_succeeded, Qt.ConnectionType.QueuedConnection)
    worker.failed.connect(_failed, Qt.ConnectionType.QueuedConnection)
    worker.submit(operation)
    loop.exec()
    dialog.finish()
    worker.stop()
    if "error" in outcome:
        error = outcome["error"]
        raise error if isinstance(error, BaseException) else MailDockError(str(error))
    if "value" not in outcome:
        raise MailDockError("Storage operation produced no result")
    return outcome["value"]
