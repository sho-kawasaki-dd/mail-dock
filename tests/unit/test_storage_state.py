"""Tests for the Qt-independent storage lifecycle state machine."""

from __future__ import annotations

import pytest

from mail_dock.domain.storage_state import StorageEvent, StorageState, StorageStateMachine


@pytest.mark.parametrize(
    ("event", "expected_state"),
    [
        (StorageEvent.PROBE_OK, StorageState.ATTACHED),
        (StorageEvent.PROBE_MISSING, StorageState.DEGRADED),
        (StorageEvent.PROBE_FOREIGN, StorageState.DETACHED),
    ],
)
def test_attached_probe_result_selects_the_safe_state(
    event: StorageEvent,
    expected_state: StorageState,
) -> None:
    machine = StorageStateMachine()

    assert machine.transition(event) is expected_state


def test_transient_io_error_recovers_after_successful_reprobe() -> None:
    machine = StorageStateMachine()

    assert machine.transition(StorageEvent.IO_ERROR) is StorageState.DEGRADED
    assert machine.transition(StorageEvent.REPROBE_OK) is StorageState.ATTACHED


@pytest.mark.parametrize(
    "event",
    [StorageEvent.REPROBE_FAILED, StorageEvent.DEVICE_REMOVED],
)
def test_degraded_storage_detaches_without_waiting_for_recovery(
    event: StorageEvent,
) -> None:
    machine = StorageStateMachine()
    machine.transition(StorageEvent.IO_ERROR)

    assert machine.transition(event) is StorageState.DETACHED


def test_user_detach_has_a_distinct_state() -> None:
    machine = StorageStateMachine()

    assert machine.transition(StorageEvent.USER_DETACH) is StorageState.DETACHED_BY_USER


def test_device_arrival_reconnects_and_verifies_identity() -> None:
    machine = StorageStateMachine(StorageState.DETACHED)

    assert machine.transition(StorageEvent.DEVICE_ARRIVED) is StorageState.RECONNECTING
    assert machine.transition(StorageEvent.IDENTITY_OK) is StorageState.VERIFYING
    assert machine.transition(StorageEvent.VERIFY_OK) is StorageState.ATTACHED


def test_failed_verification_detaches_again() -> None:
    machine = StorageStateMachine(StorageState.RECONNECTING)
    machine.transition(StorageEvent.IDENTITY_OK)

    assert machine.transition(StorageEvent.VERIFY_FAILED) is StorageState.DETACHED


def test_foreign_identity_detaches_without_authorizing_writes() -> None:
    machine = StorageStateMachine(StorageState.RECONNECTING)

    assert machine.transition(StorageEvent.IDENTITY_FOREIGN) is StorageState.DETACHED
    assert not machine.is_write_allowed()
    assert not machine.is_remote_delete_allowed()


@pytest.mark.parametrize("state", list(StorageState))
def test_only_attached_state_allows_writes_and_remote_deletion(
    state: StorageState,
) -> None:
    machine = StorageStateMachine(state)

    assert machine.is_write_allowed() is (state is StorageState.ATTACHED)
    assert machine.is_remote_delete_allowed() is (state is StorageState.ATTACHED)


def test_invalid_transition_does_not_change_state() -> None:
    machine = StorageStateMachine()

    with pytest.raises(ValueError):
        machine.transition(StorageEvent.VERIFY_OK)

    assert machine.state is StorageState.ATTACHED
