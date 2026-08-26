"""Qt-independent state machine for the local mail storage root."""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar


class StorageState(StrEnum):
    """Lifecycle states of the configured storage root."""

    ATTACHED = "attached"
    DEGRADED = "degraded"
    DETACHED = "detached"
    DETACHED_BY_USER = "detached_by_user"
    RECONNECTING = "reconnecting"
    VERIFYING = "verifying"


class StorageEvent(StrEnum):
    """Events that can change the storage-root lifecycle state."""

    PROBE_OK = "probe_ok"
    PROBE_MISSING = "probe_missing"
    PROBE_FOREIGN = "probe_foreign"
    IO_ERROR = "io_error"
    REPROBE_OK = "reprobe_ok"
    REPROBE_FAILED = "reprobe_failed"
    DEVICE_REMOVED = "device_removed"
    DEVICE_ARRIVED = "device_arrived"
    USER_DETACH = "user_detach"
    RECONNECT_REQUESTED = "reconnect_requested"
    IDENTITY_OK = "identity_ok"
    IDENTITY_FOREIGN = "identity_foreign"
    VERIFY_OK = "verify_ok"
    VERIFY_FAILED = "verify_failed"


class StorageStateMachine:
    """Apply the storage disconnect and reconnect transition table.

    The machine deliberately does not perform any I/O or UI work. Callers
    notify it about observations and own the side effects associated with a
    resulting state change. Events that are not valid for the current state
    raise ``ValueError`` so an incorrectly wired monitor cannot silently
    authorize a write.
    """

    _TRANSITIONS: ClassVar[dict[tuple[StorageState, StorageEvent], StorageState]] = {
        (StorageState.ATTACHED, StorageEvent.PROBE_OK): StorageState.ATTACHED,
        (StorageState.ATTACHED, StorageEvent.PROBE_MISSING): StorageState.DEGRADED,
        (StorageState.ATTACHED, StorageEvent.PROBE_FOREIGN): StorageState.DETACHED,
        (StorageState.ATTACHED, StorageEvent.IO_ERROR): StorageState.DEGRADED,
        (StorageState.ATTACHED, StorageEvent.DEVICE_REMOVED): StorageState.DETACHED,
        (StorageState.ATTACHED, StorageEvent.USER_DETACH): StorageState.DETACHED_BY_USER,
        (StorageState.DEGRADED, StorageEvent.PROBE_MISSING): StorageState.DEGRADED,
        (StorageState.DEGRADED, StorageEvent.PROBE_FOREIGN): StorageState.DETACHED,
        (StorageState.DEGRADED, StorageEvent.IO_ERROR): StorageState.DEGRADED,
        (StorageState.DEGRADED, StorageEvent.REPROBE_OK): StorageState.ATTACHED,
        (StorageState.DEGRADED, StorageEvent.REPROBE_FAILED): StorageState.DETACHED,
        (StorageState.DEGRADED, StorageEvent.DEVICE_REMOVED): StorageState.DETACHED,
        (StorageState.DEGRADED, StorageEvent.USER_DETACH): StorageState.DETACHED_BY_USER,
        (StorageState.DETACHED, StorageEvent.PROBE_MISSING): StorageState.DETACHED,
        (StorageState.DETACHED, StorageEvent.PROBE_FOREIGN): StorageState.DETACHED,
        (StorageState.DETACHED, StorageEvent.IO_ERROR): StorageState.DETACHED,
        (StorageState.DETACHED, StorageEvent.DEVICE_REMOVED): StorageState.DETACHED,
        (StorageState.DETACHED, StorageEvent.DEVICE_ARRIVED): StorageState.RECONNECTING,
        (StorageState.DETACHED, StorageEvent.RECONNECT_REQUESTED): StorageState.RECONNECTING,
        (StorageState.DETACHED_BY_USER, StorageEvent.PROBE_MISSING): StorageState.DETACHED_BY_USER,
        (StorageState.DETACHED_BY_USER, StorageEvent.PROBE_FOREIGN): StorageState.DETACHED_BY_USER,
        (StorageState.DETACHED_BY_USER, StorageEvent.DEVICE_REMOVED): StorageState.DETACHED_BY_USER,
        (StorageState.DETACHED_BY_USER, StorageEvent.RECONNECT_REQUESTED): (
            StorageState.RECONNECTING
        ),
        (StorageState.RECONNECTING, StorageEvent.PROBE_FOREIGN): StorageState.DETACHED,
        (StorageState.RECONNECTING, StorageEvent.DEVICE_REMOVED): StorageState.DETACHED,
        (StorageState.RECONNECTING, StorageEvent.DEVICE_ARRIVED): StorageState.RECONNECTING,
        (StorageState.RECONNECTING, StorageEvent.RECONNECT_REQUESTED): StorageState.RECONNECTING,
        (StorageState.RECONNECTING, StorageEvent.IDENTITY_OK): StorageState.VERIFYING,
        (StorageState.RECONNECTING, StorageEvent.IDENTITY_FOREIGN): StorageState.DETACHED,
        (StorageState.VERIFYING, StorageEvent.PROBE_FOREIGN): StorageState.DETACHED,
        (StorageState.VERIFYING, StorageEvent.DEVICE_REMOVED): StorageState.DETACHED,
        (StorageState.VERIFYING, StorageEvent.VERIFY_OK): StorageState.ATTACHED,
        (StorageState.VERIFYING, StorageEvent.VERIFY_FAILED): StorageState.DETACHED,
    }

    def __init__(self, initial_state: StorageState = StorageState.ATTACHED) -> None:
        """Create a machine, normally starting with an attached root."""

        self._state = initial_state

    @property
    def state(self) -> StorageState:
        """Return the current storage state."""

        return self._state

    def transition(self, event: StorageEvent) -> StorageState:
        """Apply ``event`` and return the resulting state."""

        try:
            next_state = self._TRANSITIONS[(self._state, event)]
        except KeyError as error:
            raise ValueError(f"invalid storage transition: {self._state} + {event}") from error
        self._state = next_state
        return next_state

    def is_write_allowed(self) -> bool:
        """Return whether local storage writes are currently permitted."""

        return self._state is StorageState.ATTACHED

    def is_remote_delete_allowed(self) -> bool:
        """Return whether destructive remote operations are currently permitted."""

        return self._state is StorageState.ATTACHED


__all__ = ["StorageEvent", "StorageState", "StorageStateMachine"]
