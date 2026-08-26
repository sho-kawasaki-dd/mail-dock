"""Watch Windows volume changes without coupling storage policy to Win32."""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar, cast, override

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication, QObject, Signal

from mail_dock.domain.storage_state import StorageEvent

WM_DEVICECHANGE = 0x0219
DBT_DEVICEARRIVAL = 0x8000
DBT_DEVICEQUERYREMOVE = 0x8001
DBT_DEVICEREMOVECOMPLETE = 0x8004
DBT_DEVTYP_VOLUME = 0x0002

_NATIVE_EVENT_TYPES = frozenset({b"windows_generic_MSG", b"windows_dispatcher_MSG"})
_MAX_DRIVE_UNITMASK = (1 << 26) - 1


def drive_letters_from_unitmask(unitmask: int) -> tuple[str, ...]:
    """Convert a Win32 volume unit mask into drive letters in A-to-Z order."""

    if not isinstance(unitmask, int) or isinstance(unitmask, bool):
        raise TypeError("unitmask must be an integer")
    if unitmask < 0 or unitmask > _MAX_DRIVE_UNITMASK:
        raise ValueError("unitmask must contain only A: through Z: bits")
    return tuple(
        f"{chr(ord('A') + bit_index)}:" for bit_index in range(26) if unitmask & (1 << bit_index)
    )


@dataclass(frozen=True)
class DeviceChange:
    """A native volume event normalized for presentation-layer consumers."""

    event: StorageEvent
    drive_letters: tuple[str, ...]


class _DeviceWatcherSignals(QObject):
    """QObject host for signals because the native filter is not a QObject."""

    device_query_remove = Signal(object)
    device_removed = Signal(object)
    device_arrived = Signal(object)
    event_detected = Signal(object)


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _NativeMessage(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("w_param", ctypes.c_size_t),
        ("l_param", ctypes.c_ssize_t),
        ("time", ctypes.c_uint32),
        ("point", _Point),
    ]


class _DeviceBroadcastHeader(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_uint32),
        ("device_type", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
    ]


class _DeviceBroadcastVolume(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_uint32),
        ("device_type", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
        ("unitmask", ctypes.c_uint32),
        ("flags", ctypes.c_uint16),
    ]


class DeviceWatcher(QAbstractNativeEventFilter):
    """Translate Windows volume broadcasts into storage lifecycle events."""

    _WINDOWS_EVENT_TYPES: ClassVar[frozenset[bytes]] = _NATIVE_EVENT_TYPES

    def __init__(
        self,
        on_event: Callable[[DeviceChange], None] | None = None,
        on_query_remove: Callable[[tuple[str, ...]], None] | None = None,
    ) -> None:
        super().__init__()
        self._signals = _DeviceWatcherSignals()
        self._application: QCoreApplication | None = None
        self._on_event = on_event
        self._on_query_remove = on_query_remove

    @property
    def device_query_remove(self) -> Any:
        """Signal emitted before Windows completes a requested removal."""

        return self._signals.device_query_remove

    @property
    def device_removed(self) -> Any:
        """Signal carrying drive letters after a volume removal completes."""

        return self._signals.device_removed

    @property
    def device_arrived(self) -> Any:
        """Signal carrying drive letters after a volume arrives."""

        return self._signals.device_arrived

    @property
    def event_detected(self) -> Any:
        """Signal carrying each normalized device change."""

        return self._signals.event_detected

    def install(self, application: QCoreApplication | None = None) -> None:
        """Install this filter, or safely do nothing outside Windows."""

        if sys.platform != "win32":
            return
        target = application or QCoreApplication.instance()
        if target is None or self._application is target:
            return
        if self._application is not None:
            self.uninstall()
        target.installNativeEventFilter(self)
        self._application = target

    def uninstall(self, application: QCoreApplication | None = None) -> None:
        """Remove this filter if it was installed."""

        if sys.platform != "win32":
            self._application = None
            return
        target = application or self._application
        if target is None:
            return
        target.removeNativeEventFilter(self)
        if target is self._application:
            self._application = None

    @override
    def nativeEventFilter(self, event_type: object, message: object) -> tuple[bool, int]:
        """Handle a Windows ``MSG`` pointer and never consume unrelated events."""

        if sys.platform != "win32" or not self._is_supported_event_type(event_type):
            return False, 0
        native_message = self._read_message(message)
        if native_message is None or native_message.message != WM_DEVICECHANGE:
            return False, 0

        change_type = int(native_message.w_param)
        if change_type == DBT_DEVICEQUERYREMOVE:
            drive_letters = self._read_drive_letters(native_message.l_param) or ()
            self._signals.device_query_remove.emit(drive_letters)
            if self._on_query_remove is not None:
                self._on_query_remove(drive_letters)
            return True, 1
        if change_type not in {DBT_DEVICEREMOVECOMPLETE, DBT_DEVICEARRIVAL}:
            return False, 0

        drive_letters = self._read_drive_letters(native_message.l_param)
        if drive_letters is None:
            return False, 0
        event = (
            StorageEvent.DEVICE_REMOVED
            if change_type == DBT_DEVICEREMOVECOMPLETE
            else StorageEvent.DEVICE_ARRIVED
        )
        change = DeviceChange(event, drive_letters)
        self._signals.event_detected.emit(change)
        if event is StorageEvent.DEVICE_REMOVED:
            self._signals.device_removed.emit(drive_letters)
        else:
            self._signals.device_arrived.emit(drive_letters)
        if self._on_event is not None:
            self._on_event(change)
        return False, 0

    @staticmethod
    def _is_supported_event_type(event_type: object) -> bool:
        try:
            normalized = (
                bytes(event_type)
                if isinstance(event_type, (bytes, bytearray))
                else bytes(cast(Any, event_type))
            )
        except (TypeError, ValueError):
            normalized = str(event_type).encode()
        return normalized in _NATIVE_EVENT_TYPES

    @staticmethod
    def _read_message(message: object) -> _NativeMessage | None:
        try:
            pointer_value = int(cast(Any, message))
            if pointer_value == 0:
                return None
            return ctypes.cast(pointer_value, ctypes.POINTER(_NativeMessage)).contents
        except (TypeError, ValueError, OSError):
            return None

    @staticmethod
    def _read_drive_letters(l_param: int) -> tuple[str, ...] | None:
        if not l_param:
            return None
        try:
            header = ctypes.cast(int(l_param), ctypes.POINTER(_DeviceBroadcastHeader)).contents
            if header.size < ctypes.sizeof(_DeviceBroadcastHeader):
                return None
            if header.device_type != DBT_DEVTYP_VOLUME:
                return None
            if header.size < ctypes.sizeof(_DeviceBroadcastVolume):
                return None
            volume = ctypes.cast(int(l_param), ctypes.POINTER(_DeviceBroadcastVolume)).contents
            return drive_letters_from_unitmask(int(volume.unitmask))
        except (TypeError, ValueError, OSError):
            return None
