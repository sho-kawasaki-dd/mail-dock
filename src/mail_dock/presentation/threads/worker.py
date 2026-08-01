"""Shared QObject and QThread worker infrastructure.

The worker owns no application-specific operation. Concrete workers submit
callables through :meth:`Worker.submit`, while the callable itself runs in
the worker thread. SQLite cleanup is deliberately expressed as a tiny
protocol so this module does not depend on the infrastructure layer.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import Protocol

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot

from mail_dock.domain.errors import MailDockError, OperationCancelledError
from mail_dock.domain.fetcher import CancelToken

LOGGER = logging.getLogger(__name__)


class _ConnectionCloser(Protocol):
    """Minimal connection-manager boundary needed during worker shutdown."""

    def close_current_thread(self) -> None:
        """Close the SQLite connection owned by the calling thread."""


@dataclass(frozen=True)
class _Task:
    """A queued operation and the token owned by its UI-side caller."""

    operation: Callable[[], object]
    token: CancelToken


class Worker(QObject):
    """Run submitted operations on a dedicated, reusable QThread.

    ``submit`` is safe to call from the GUI thread and returns the exact
    ``CancelToken`` used by the operation. The caller can therefore invoke
    ``token.cancel()`` directly while the operation is inside a blocking
    slot; cancellation does not depend on the worker thread processing a
    queued cancellation slot.

    A worker processes one operation at a time. ``result`` and ``failed`` are
    emitted from the worker thread and are delivered to GUI receivers through
    Qt's normal queued-signal rules. Exceptions never escape the worker slot.
    """

    task_requested = Signal(object)
    result = Signal(object)
    failed = Signal(object)
    cancelled = Signal()
    task_started = Signal()
    task_finished = Signal()
    task_completed = Signal(object)
    task_failed = Signal(object, object)
    task_cancelled = Signal(object)
    stopped = Signal()

    def __init__(
        self,
        connection_manager: _ConnectionCloser | None = None,
    ) -> None:
        super().__init__()
        self._connection_manager = connection_manager
        self._state_lock = Lock()
        self._tokens: set[CancelToken] = set()
        self._stopping = False
        self._started = False
        self._stopped = False

        self._thread = QThread()
        self._thread.setObjectName(f"{type(self).__name__}Thread")
        self.moveToThread(self._thread)
        self.task_requested.connect(
            self._execute_task,
            Qt.ConnectionType.QueuedConnection,
        )

    @property
    def qthread(self) -> QThread:
        """Return the dedicated thread managed by this worker."""

        return self._thread

    @property
    def active_tokens(self) -> tuple[CancelToken, ...]:
        """Return a snapshot of tokens for operations not yet completed."""

        with self._state_lock:
            return tuple(self._tokens)

    def start(self) -> None:
        """Start the worker thread before submitting operations."""

        with self._state_lock:
            if self._stopping:
                raise RuntimeError("worker has already been stopped")
            if self._started:
                return
            self._started = True
        self._thread.start()

    def submit(
        self,
        operation: Callable[[], object],
        token: CancelToken | None = None,
    ) -> CancelToken:
        """Queue ``operation`` and return its caller-owned cancellation token."""

        if not callable(operation):
            raise TypeError("worker operation must be callable")
        operation_token = token if token is not None else CancelToken()
        with self._state_lock:
            if self._stopping:
                raise RuntimeError("worker is stopping")
            if not self._started or not self._thread.isRunning():
                raise RuntimeError("worker thread has not been started")
            self._tokens.add(operation_token)
        try:
            self.task_requested.emit(_Task(operation, operation_token))
        except BaseException:
            with self._state_lock:
                self._tokens.discard(operation_token)
            raise
        return operation_token

    def cancel_all(self) -> None:
        """Cancel every queued or running operation without using Qt events."""

        for token in self.active_tokens:
            token.cancel()

    def stop(self) -> None:
        """Cancel operations and wait until the dedicated thread has exited."""

        if self._thread.isRunning() and QThread.currentThread() == self._thread:
            raise RuntimeError("worker.stop() must be called outside the worker thread")
        with self._state_lock:
            if self._stopped:
                return
            self._stopping = True
        self.cancel_all()

        if not self._thread.isRunning():
            with self._state_lock:
                self._stopped = True
            self.stopped.emit()
            return

        self._thread.quit()
        self._thread.wait()
        with self._state_lock:
            self._tokens.clear()
            self._stopped = True
        self.stopped.emit()

    @Slot(object)
    def _execute_task(self, task: object) -> None:
        """Execute one queued task and convert all expected failures to signals."""

        if not isinstance(task, _Task):
            self.failed.emit(MailDockError("Invalid worker task"))
            return

        with self._state_lock:
            if self._stopping or task.token not in self._tokens:
                self._tokens.discard(task.token)
                return

        self.task_started.emit()
        task_error: MailDockError | None = None
        try:
            task.token.raise_if_cancelled()
            value = task.operation()
        except OperationCancelledError:
            self._emit_task_cancelled(task)
        except Exception as error:
            task_error = _as_mail_dock_error(error)
            LOGGER.exception("Worker task failed")
            self._emit_task_failed(task, task_error)
        else:
            self._emit_task_result(task, value)
        finally:
            cleanup_error = self._close_connection()
            with self._state_lock:
                self._tokens.discard(task.token)
            if cleanup_error is not None and task_error is None:
                self._emit_task_failed(task, cleanup_error)
            self.task_finished.emit()
            self.task_completed.emit(task.token)

    def _emit_task_result(self, task: _Task, value: object) -> None:
        """Emit a successful result; subclasses may filter stale requests."""

        self.result.emit(value)

    def _emit_task_failed(self, task: _Task, error: MailDockError) -> None:
        """Emit a failure and retain the task identity for specialized workers."""

        self.failed.emit(error)
        self.task_failed.emit(task.token, error)

    def _emit_task_cancelled(self, task: _Task) -> None:
        """Emit cancellation and retain the task identity for specialized workers."""

        self.cancelled.emit()
        self.task_cancelled.emit(task.token)

    def _close_connection(self) -> MailDockError | None:
        """Close this thread's database connection, if one was supplied."""

        if self._connection_manager is None:
            return None
        try:
            self._connection_manager.close_current_thread()
        except Exception as error:
            normalized = _as_mail_dock_error(error)
            LOGGER.exception("Worker connection cleanup failed")
            return normalized
        return None


BaseWorker = Worker
WorkerBase = Worker


def _as_mail_dock_error(error: Exception) -> MailDockError:
    """Keep domain errors intact and normalize foreign exceptions."""

    if isinstance(error, MailDockError):
        return error
    message = str(error).strip() or type(error).__name__
    return MailDockError(message)
