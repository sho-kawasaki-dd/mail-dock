"""Monitor the active storage root and coordinate disconnect handling."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from mail_dock import config
from mail_dock.domain.storage_state import StorageEvent, StorageState, StorageStateMachine
from mail_dock.infrastructure.logging_config import (
    set_application_log_target,
    set_storage_log_target,
)
from mail_dock.infrastructure.storage.storage_root import RootProbe, probe

LOGGER = logging.getLogger(__name__)

ProbeFunc = Callable[[Path, str | None], RootProbe]
ReconnectFunc = Callable[[], None]


class StorageMonitor(QObject):
    """Keep the GUI storage lifecycle in sync with the physical root.

    The monitor owns only Qt timers and lifecycle side effects. The transition
    table itself lives in :class:`StorageStateMachine`, which keeps the policy
    testable without Qt. ``workers`` are expected to expose the existing
    ``storage_detached`` signal and the ``cancel_all``/``stop`` methods from
    ``Worker``; duck typing keeps this class usable with test doubles.
    """

    storage_state_changed = Signal(object)
    storage_detached = Signal(object)

    def __init__(
        self,
        root: Path,
        root_uuid: str | None,
        settings: config.AppConfig,
        *,
        storage_lock: Any | None = None,
        connection_manager: Any | None = None,
        workers: Iterable[object] = (),
        reconnect: ReconnectFunc | None = None,
        probe_func: ProbeFunc = probe,
        config_log_dir: Path | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.root = root
        self.root_uuid = root_uuid
        self.settings = settings
        self.storage_lock = storage_lock
        self.connection_manager = connection_manager
        self.workers = tuple(workers)
        self.reconnect = reconnect
        self._probe = probe_func
        self._config_log_dir = config_log_dir
        self._machine = StorageStateMachine()
        self._reprobe_count = 0
        self._detached_handled = False

        self.heartbeat_timer = QTimer(self)
        self.heartbeat_timer.setInterval(settings.heartbeat_interval_sec * 1000)
        self.heartbeat_timer.timeout.connect(self.heartbeat)

        self.reprobe_timer = QTimer(self)
        self.reprobe_timer.setSingleShot(True)
        self.reprobe_timer.setInterval(500)
        self.reprobe_timer.timeout.connect(self._reprobe)

        for worker in self.workers:
            signal = getattr(worker, "storage_detached", None)
            connect = getattr(signal, "connect", None)
            if callable(connect):
                connect(self.handle_io_error)

    @property
    def state(self) -> StorageState:
        """Return the current storage lifecycle state."""

        return self._machine.state

    @property
    def reprobe_count(self) -> int:
        """Return the number of reprobes attempted for the current outage."""

        return self._reprobe_count

    def start(self) -> None:
        """Start heartbeat monitoring for the attached root."""

        if self.state is StorageState.ATTACHED:
            self.heartbeat_timer.start()

    def stop(self) -> None:
        """Stop timers without touching storage or worker resources."""

        self.heartbeat_timer.stop()
        self.reprobe_timer.stop()

    @Slot()
    def heartbeat(self) -> None:
        """Probe the marker and publish the result to the state machine."""

        if self.state not in {StorageState.ATTACHED, StorageState.DEGRADED}:
            return
        try:
            result = self._probe(self.root, self.root_uuid)
        except Exception as error:
            LOGGER.warning("Storage heartbeat probe failed: %s", error)
            self._transition(StorageEvent.IO_ERROR, error)
            self._reprobe_count = 0
            self._schedule_reprobe()
            return

        event = {
            RootProbe.OK: StorageEvent.PROBE_OK,
            RootProbe.MISSING: StorageEvent.PROBE_MISSING,
            RootProbe.FOREIGN: StorageEvent.PROBE_FOREIGN,
        }[result]
        self._transition(event, result)
        if result is RootProbe.OK:
            self._touch_heartbeat()
        elif result is RootProbe.MISSING and self.state is StorageState.DEGRADED:
            self._reprobe_count = 0
            self._schedule_reprobe()

    @Slot(object)
    def handle_io_error(self, error: object) -> None:
        """Handle a worker's storage I/O failure as a possible transient loss."""

        if self.state in {StorageState.DETACHED, StorageState.DETACHED_BY_USER}:
            return
        self._transition(StorageEvent.IO_ERROR, error)
        if self.state is StorageState.DEGRADED:
            self._reprobe_count = 0
            self._schedule_reprobe()

    def request_reconnect(self) -> None:
        """Request a reconnect after a detached root becomes available."""

        if self.state not in {StorageState.DETACHED, StorageState.DETACHED_BY_USER}:
            return
        self._transition(StorageEvent.RECONNECT_REQUESTED)
        self._reprobe_count = 0
        self._schedule_reprobe()

    def handle_device_arrived(self) -> None:
        """Start reconnecting after a native device-arrival notification."""

        if self.state is StorageState.DETACHED:
            self._transition(StorageEvent.DEVICE_ARRIVED)
            self._reprobe_count = 0
            self._schedule_reprobe()

    def handle_device_removed(self) -> None:
        """Enter detached state immediately for a completed device removal."""

        if self.state is not StorageState.DETACHED:
            self._transition(StorageEvent.DEVICE_REMOVED)

    def mark_detached_by_user(self) -> None:
        """Publish the final state after the user-initiated release completed."""

        if self.state in {StorageState.ATTACHED, StorageState.DEGRADED}:
            self._transition(StorageEvent.USER_DETACH)

    def _schedule_reprobe(self) -> None:
        if self.reprobe_timer.isActive():
            return
        self.reprobe_timer.start(500)

    @Slot()
    def _reprobe(self) -> None:
        """Retry identity probing at the fixed 500ms transient-loss cadence."""

        if self.state not in {StorageState.DEGRADED, StorageState.RECONNECTING}:
            return
        self._reprobe_count += 1
        try:
            result = self._probe(self.root, self.root_uuid)
        except Exception as error:
            LOGGER.warning("Storage reprobe failed: %s", error)
            result = RootProbe.MISSING

        if result is RootProbe.OK:
            if self.state is StorageState.DEGRADED:
                self._transition(StorageEvent.REPROBE_OK, result)
                self._touch_heartbeat()
                self._reprobe_count = 0
                self.heartbeat_timer.start()
                self._notify_reconnected()
            else:
                self._transition(StorageEvent.IDENTITY_OK, result)
                self._notify_reconnected()
            return
        if result is RootProbe.FOREIGN:
            if self.state is StorageState.RECONNECTING:
                self._transition(StorageEvent.IDENTITY_FOREIGN, result)
            else:
                self._transition(StorageEvent.PROBE_FOREIGN, result)
            return

        attempts = self.settings.reprobe_attempts
        if self._reprobe_count >= attempts:
            self._transition(StorageEvent.REPROBE_FAILED, result)
            return
        self._schedule_reprobe()

    def _transition(self, event: StorageEvent, detail: object | None = None) -> None:
        previous = self.state
        try:
            current = self._machine.transition(event)
        except ValueError:
            LOGGER.debug("Ignoring storage event %s in state %s", event, previous)
            return
        if current == previous:
            return
        self.storage_state_changed.emit(current)
        if current is StorageState.DETACHED:
            self._handle_detached(detail)

    def _handle_detached(self, detail: object | None) -> None:
        if self._detached_handled:
            return
        self._detached_handled = True
        self.stop()
        for worker in self.workers:
            cancel_all = getattr(worker, "cancel_all", None)
            if callable(cancel_all):
                cancel_all()
            stop = getattr(worker, "stop", None)
            if callable(stop):
                stop()
        manager = self.connection_manager
        request_close_all = getattr(manager, "request_close_all", None)
        if callable(request_close_all):
            request_close_all()
        close_current_thread = getattr(manager, "close_current_thread", None)
        if callable(close_current_thread):
            close_current_thread()
        assert_all_closed = getattr(manager, "assert_all_closed", None)
        if callable(assert_all_closed):
            try:
                assert_all_closed()
            except Exception:
                LOGGER.exception("SQLite connections remain after storage detachment")
        if self._config_log_dir is None:
            set_storage_log_target(None)
        else:
            set_application_log_target(self._config_log_dir)
        LOGGER.error("Storage detached: %s", detail)
        self.storage_detached.emit(detail)

    def _notify_reconnected(self) -> None:
        """Let the composition root replace stopped resources after recovery."""

        if self.reconnect is not None:
            self.reconnect()

    def _touch_heartbeat(self) -> None:
        if self.storage_lock is None:
            return
        try:
            self.storage_lock.touch_heartbeat()
        except Exception as error:
            LOGGER.warning("Storage heartbeat update failed: %s", error)
            self._transition(StorageEvent.IO_ERROR, error)
            if self.state is StorageState.DEGRADED:
                self._reprobe_count = 0
                self._schedule_reprobe()


__all__ = ["StorageMonitor"]
