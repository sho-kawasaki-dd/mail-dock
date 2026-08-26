"""Tests for the platform-neutral parts of the native device watcher."""

from __future__ import annotations

import ctypes
import sys

import pytest
from PySide6.QtCore import QCoreApplication

from mail_dock.domain.storage_state import StorageEvent
from mail_dock.presentation.native import device_watcher
from mail_dock.presentation.native.device_watcher import (
    DBT_DEVICEARRIVAL,
    DBT_DEVICEQUERYREMOVE,
    DBT_DEVICEREMOVECOMPLETE,
    DBT_DEVTYP_VOLUME,
    WM_DEVICECHANGE,
    DeviceWatcher,
    drive_letters_from_unitmask,
)


def test_drive_letters_from_unitmask_supports_empty_single_and_multiple_masks() -> None:
    assert drive_letters_from_unitmask(0) == ()
    assert drive_letters_from_unitmask(1 << 4) == ("E:",)
    assert drive_letters_from_unitmask((1 << 0) | (1 << 2) | (1 << 25)) == (
        "A:",
        "C:",
        "Z:",
    )


@pytest.mark.parametrize("unitmask", [-1, 1 << 26])
def test_drive_letters_from_unitmask_rejects_bits_outside_drive_letters(unitmask: int) -> None:
    with pytest.raises(ValueError):
        drive_letters_from_unitmask(unitmask)


def test_device_watcher_is_a_safe_noop_on_non_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    watcher = DeviceWatcher()

    watcher.install()
    watcher.uninstall()
    assert watcher.nativeEventFilter(b"windows_generic_MSG", 0) == (False, 0)


def test_native_filter_ignores_unsupported_events(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    watcher = DeviceWatcher()

    assert watcher.nativeEventFilter(b"not-a-windows-event", 0) == (False, 0)
    assert watcher.nativeEventFilter(b"windows_generic_MSG", 0) == (False, 0)


@pytest.mark.parametrize(
    ("change_type", "expected_event"),
    [
        (DBT_DEVICEARRIVAL, StorageEvent.DEVICE_ARRIVED),
        (DBT_DEVICEREMOVECOMPLETE, StorageEvent.DEVICE_REMOVED),
    ],
)
def test_native_filter_forwards_volume_changes(
    monkeypatch: pytest.MonkeyPatch,
    change_type: int,
    expected_event: StorageEvent,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    watcher = DeviceWatcher()
    received: list[device_watcher.DeviceChange] = []
    received_letters: list[tuple[str, ...]] = []
    watcher.event_detected.connect(received.append)
    watcher.device_arrived.connect(received_letters.append)
    watcher.device_removed.connect(received_letters.append)

    volume = device_watcher._DeviceBroadcastVolume()
    volume.size = ctypes.sizeof(volume)
    volume.device_type = DBT_DEVTYP_VOLUME
    volume.unitmask = (1 << 3) | (1 << 25)
    message = device_watcher._NativeMessage()
    message.message = WM_DEVICECHANGE
    message.w_param = change_type
    message.l_param = ctypes.addressof(volume)

    assert watcher.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(message)) == (
        False,
        0,
    )
    assert received == [device_watcher.DeviceChange(expected_event, ("D:", "Z:"))]
    assert received_letters == [("D:", "Z:")]


def test_native_filter_allows_requested_removal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    closed: list[tuple[str, ...]] = []
    watcher = DeviceWatcher(on_query_remove=closed.append)
    received: list[tuple[str, ...]] = []
    watcher.device_query_remove.connect(received.append)

    volume = device_watcher._DeviceBroadcastVolume()
    volume.size = ctypes.sizeof(volume)
    volume.device_type = DBT_DEVTYP_VOLUME
    volume.unitmask = 1 << 4
    message = device_watcher._NativeMessage()
    message.message = WM_DEVICECHANGE
    message.w_param = DBT_DEVICEQUERYREMOVE
    message.l_param = ctypes.addressof(volume)

    assert watcher.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(message)) == (
        True,
        1,
    )
    assert received == [("E:",)]
    assert closed == [("E:",)]


def test_native_filter_ignores_non_volume_broadcast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    watcher = DeviceWatcher()
    received: list[device_watcher.DeviceChange] = []
    watcher.event_detected.connect(received.append)

    header = device_watcher._DeviceBroadcastHeader()
    header.size = ctypes.sizeof(header)
    header.device_type = 1
    message = device_watcher._NativeMessage()
    message.message = WM_DEVICECHANGE
    message.w_param = DBT_DEVICEREMOVECOMPLETE
    message.l_param = ctypes.addressof(header)

    assert watcher.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(message)) == (
        False,
        0,
    )
    assert received == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ABI test")
def test_windows_ctypes_structures_match_win32_layout() -> None:
    assert ctypes.sizeof(device_watcher._DeviceBroadcastHeader) == 12
    assert ctypes.sizeof(device_watcher._DeviceBroadcastVolume) == 20

    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(device_watcher._NativeMessage) == 48
        assert device_watcher._NativeMessage.message.offset == 8
        assert device_watcher._NativeMessage.w_param.offset == 16
        assert device_watcher._NativeMessage.l_param.offset == 24
        assert device_watcher._NativeMessage.time.offset == 32
        assert device_watcher._NativeMessage.point.offset == 40
    else:
        assert ctypes.sizeof(device_watcher._NativeMessage) == 28
        assert device_watcher._NativeMessage.message.offset == 4
        assert device_watcher._NativeMessage.w_param.offset == 8
        assert device_watcher._NativeMessage.l_param.offset == 12
        assert device_watcher._NativeMessage.time.offset == 16
        assert device_watcher._NativeMessage.point.offset == 20


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native event test")
def test_windows_native_filter_reads_real_ctypes_message() -> None:
    watcher = DeviceWatcher()
    received: list[device_watcher.DeviceChange] = []
    watcher.event_detected.connect(received.append)

    volume = device_watcher._DeviceBroadcastVolume()
    volume.size = ctypes.sizeof(volume)
    volume.device_type = DBT_DEVTYP_VOLUME
    volume.unitmask = 1 << 4
    message = device_watcher._NativeMessage()
    message.message = WM_DEVICECHANGE
    message.w_param = DBT_DEVICEREMOVECOMPLETE
    message.l_param = ctypes.addressof(volume)

    assert watcher.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(message)) == (
        False,
        0,
    )
    assert received == [device_watcher.DeviceChange(StorageEvent.DEVICE_REMOVED, ("E:",))]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Qt integration test")
def test_windows_watcher_registers_and_unregisters_with_qt() -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    watcher = DeviceWatcher()

    watcher.install(application)
    watcher.install(application)
    watcher.uninstall(application)
    watcher.uninstall(application)
