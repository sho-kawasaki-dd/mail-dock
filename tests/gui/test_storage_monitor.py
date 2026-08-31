from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QObject, Signal

from mail_dock import config
from mail_dock.domain.storage_state import StorageState
from mail_dock.infrastructure.storage.storage_root import RootProbe
from mail_dock.presentation.storage_monitor import StorageMonitor

pytestmark = pytest.mark.gui


class _Worker(QObject):
    storage_detached = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.cancel_all_calls = 0
        self.stop_calls = 0

    def cancel_all(self) -> None:
        self.cancel_all_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1


class _Lock:
    def __init__(self) -> None:
        self.touch_calls = 0

    def touch_heartbeat(self) -> None:
        self.touch_calls += 1


class _ConnectionManager:
    def __init__(self) -> None:
        self.request_close_all_calls = 0
        self.close_current_thread_calls = 0

    def request_close_all(self) -> None:
        self.request_close_all_calls += 1

    def close_current_thread(self) -> None:
        self.close_current_thread_calls += 1


def _monitor(
    tmp_storage_root: Path,
    probe_func: Callable[[Path, str | None], RootProbe],
    *,
    worker: _Worker | None = None,
    settings: config.AppConfig | None = None,
    config_log_dir: Path | None = None,
) -> tuple[StorageMonitor, _Lock, _ConnectionManager, _Worker]:
    active_worker = worker or _Worker()
    lock = _Lock()
    manager = _ConnectionManager()
    monitor = StorageMonitor(
        tmp_storage_root,
        "root-uuid",
        settings or config.AppConfig(reprobe_attempts=2),
        storage_lock=lock,
        connection_manager=manager,
        workers=(active_worker,),
        probe_func=probe_func,
        config_log_dir=config_log_dir,
    )
    return monitor, lock, manager, active_worker


def test_heartbeat_touches_lock_only_after_identity_probe(
    qtbot: Any, tmp_storage_root: Path
) -> None:
    del qtbot
    monitor, lock, _manager, _worker = _monitor(
        tmp_storage_root,
        lambda _root, _uuid: RootProbe.OK,
    )

    monitor.heartbeat()

    assert monitor.state is StorageState.ATTACHED
    assert lock.touch_calls == 1


def test_timers_use_configured_heartbeat_and_fixed_reprobe_intervals(
    qtbot: Any, tmp_storage_root: Path
) -> None:
    del qtbot
    monitor, _lock, _manager, _worker = _monitor(
        tmp_storage_root,
        lambda _root, _uuid: RootProbe.OK,
        settings=config.AppConfig(heartbeat_interval_sec=7, reprobe_attempts=2),
    )

    assert monitor.heartbeat_timer.interval() == 7_000
    assert monitor.reprobe_timer.interval() == 500


def test_transient_io_error_reprobes_and_returns_to_attached(
    qtbot: Any, tmp_storage_root: Path
) -> None:
    del qtbot
    results: Iterator[RootProbe] = iter((RootProbe.MISSING, RootProbe.OK))
    reconnect_calls: list[bool] = []
    monitor, lock, manager, worker = _monitor(
        tmp_storage_root,
        lambda _root, _uuid: next(results),
    )
    monitor.reconnect = lambda: reconnect_calls.append(True)

    monitor.heartbeat()
    monitor.handle_io_error(RuntimeError("transient"))
    monitor._reprobe()

    assert monitor.state is StorageState.ATTACHED
    assert monitor.reprobe_count == 0
    assert lock.touch_calls == 1
    assert reconnect_calls == [True]
    assert manager.request_close_all_calls == 0
    assert worker.stop_calls == 0


def test_missing_root_after_reprobe_attempts_detaches_and_closes_resources(
    qtbot: Any,
    tmp_storage_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del qtbot
    log_targets: list[Path | None] = []
    monkeypatch.setattr(
        "mail_dock.presentation.storage_monitor.set_storage_log_target",
        log_targets.append,
    )
    monitor, lock, manager, worker = _monitor(
        tmp_storage_root,
        lambda _root, _uuid: RootProbe.MISSING,
    )
    states: list[StorageState] = []
    monitor.storage_state_changed.connect(states.append)

    monitor.heartbeat()
    monitor._reprobe()
    monitor._reprobe()

    assert monitor.state is StorageState.DETACHED
    assert states == [StorageState.DEGRADED, StorageState.DETACHED]
    assert lock.touch_calls == 0
    assert worker.cancel_all_calls == 1
    assert worker.stop_calls == 1
    assert manager.request_close_all_calls == 1
    assert manager.close_current_thread_calls == 1
    assert log_targets == [None]


def test_missing_root_detaches_only_after_three_reprobe_attempts(
    qtbot: Any,
    tmp_storage_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del qtbot
    monkeypatch.setattr(
        "mail_dock.presentation.storage_monitor.set_storage_log_target",
        lambda _target: None,
    )
    monitor, _lock, _manager, _worker = _monitor(
        tmp_storage_root,
        lambda _root, _uuid: RootProbe.MISSING,
        settings=config.AppConfig(reprobe_attempts=3),
    )

    monitor.heartbeat()
    assert monitor.state is StorageState.DEGRADED

    monitor._reprobe()
    state_after_first_reprobe = monitor.state
    assert state_after_first_reprobe is StorageState.DEGRADED
    assert monitor.reprobe_count == 1

    monitor._reprobe()
    state_after_second_reprobe = monitor.state
    assert state_after_second_reprobe is StorageState.DEGRADED
    assert monitor.reprobe_count == 2

    monitor._reprobe()

    state_after_third_reprobe: StorageState = monitor.state
    assert state_after_third_reprobe is StorageState.DETACHED
    assert monitor.reprobe_count == 3


def test_foreign_root_detaches_without_reprobe(
    qtbot: Any,
    tmp_storage_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del qtbot
    monkeypatch.setattr(
        "mail_dock.presentation.storage_monitor.set_storage_log_target",
        lambda _target: None,
    )
    monitor, _lock, _manager, worker = _monitor(
        tmp_storage_root,
        lambda _root, _uuid: RootProbe.FOREIGN,
    )

    monitor.heartbeat()

    assert monitor.state is StorageState.DETACHED
    assert monitor.reprobe_count == 0
    assert worker.stop_calls == 1


def test_detach_switches_storage_logging_to_local_config_directory(
    qtbot: Any,
    tmp_storage_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del qtbot
    log_targets: list[Path] = []
    monkeypatch.setattr(
        "mail_dock.presentation.storage_monitor.set_application_log_target",
        log_targets.append,
    )
    monitor, _lock, _manager, _worker = _monitor(
        tmp_storage_root,
        lambda _root, _uuid: RootProbe.FOREIGN,
        config_log_dir=tmp_path / "config",
    )

    monitor.heartbeat()

    assert log_targets == [tmp_path / "config"]


def test_worker_storage_signal_enters_degraded_state(qtbot: Any, tmp_storage_root: Path) -> None:
    del qtbot
    monitor, _lock, _manager, worker = _monitor(
        tmp_storage_root,
        lambda _root, _uuid: RootProbe.MISSING,
    )

    worker.storage_detached.emit(RuntimeError("detached"))

    assert monitor.state is StorageState.DEGRADED
    monitor.stop()
