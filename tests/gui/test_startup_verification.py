from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from PySide6.QtWidgets import QApplication

from mail_dock import config
from mail_dock.presentation import app

pytestmark = pytest.mark.gui


class _ConnectionManager:
    def __init__(self) -> None:
        self.connection = object()
        self.closed = 0

    def get_connection(self) -> object:
        return self.connection

    def close_current_thread(self) -> None:
        self.closed += 1


class _Session:
    def __init__(self, mode: str, root: Path) -> None:
        self.settings = replace(config.AppConfig(), startup_verification=mode)
        self.root = root
        self.network_drive = False
        self.connection_manager = _ConnectionManager()


class _Window:
    def __init__(self) -> None:
        self.shown = False

    def show(self) -> None:
        self.shown = True


def test_startup_verification_runs_quick_and_full_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(app, "_verify_database", lambda _connection: calls.append("quick"))
    monkeypatch.setattr(
        app,
        "_verify_fts_database",
        lambda _path, *, network_drive: calls.append(f"full:{network_drive}"),
    )

    quick = _Session("quick", tmp_path)
    quick_worker = app._StartupVerificationWorker(cast(Any, quick), "quick")
    quick_worker.run()
    assert calls == ["quick"]
    assert quick.connection_manager.closed == 1

    full = _Session("full", tmp_path)
    full_worker = app._StartupVerificationWorker(cast(Any, full), "full")
    full_worker.run()
    assert calls == ["quick", "quick", "full:False"]
    assert full.connection_manager.closed == 1


def test_main_window_is_built_only_after_verification_finishes(
    tmp_path: Path,
    qtbot: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session("quick", tmp_path)
    window = _Window()
    built: list[_Window] = []

    monkeypatch.setattr(app, "_verify_database", lambda _connection: None)
    context = type(
        "Context",
        (),
        {},
    )()

    def build_main_window() -> _Window:
        built.append(window)
        return window

    context.build_main_window = build_main_window
    application = QApplication.instance()
    assert application is not None
    _thread, result = app._start_verification(
        application, cast(Any, session), cast(Any, context)
    )

    assert result["window"] is None
    assert built == []
    qtbot.waitUntil(lambda: bool(built), timeout=2_000)
    assert result["window"] is window
    assert window.shown