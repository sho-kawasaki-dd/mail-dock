from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest

from mail_dock import config
from mail_dock.domain.errors import StorageUnsupportedError
from mail_dock.presentation import app

pytestmark = pytest.mark.gui


class _FakeApplication:
    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.quit_calls = 0

    @staticmethod
    def instance() -> _FakeApplication:
        return _APPLICATION

    def exec(self) -> int:
        return self.exit_code

    def quit(self) -> None:
        self.quit_calls += 1


class _FakeSession:
    instances: ClassVar[list[_FakeSession]] = []
    fail_enter: ClassVar[bool] = False
    unsupported_remaining: ClassVar[int] = 0

    def __init__(self, _settings: config.AppConfig, root: Path) -> None:
        self.root = root
        self.root_uuid = "root-uuid"
        self.enter_calls = 0
        self.exit_calls = 0
        self.settings = _settings
        self.connection_manager = object()
        self.network_drive = False
        self.journal_mode = "WAL"
        self.__class__.instances.append(self)

    def __enter__(self) -> _FakeSession:
        self.enter_calls += 1
        if self.fail_enter:
            raise RuntimeError("session start failed")
        if self.unsupported_remaining > 0:
            self.__class__.unsupported_remaining -= 1
            raise StorageUnsupportedError(self.root_uuid, "unsupported")
        return self

    def __exit__(self, *_args: object) -> None:
        self.exit_calls += 1


class _FakeContext:
    instances: ClassVar[list[_FakeContext]] = []

    def __init__(self, session: _FakeSession, _settings: config.AppConfig) -> None:
        self.session = session
        self.save_calls = 0
        self.stop_calls = 0
        self.__class__.instances.append(self)

    def save_settings(self, _settings: config.AppConfig) -> None:
        self.save_calls += 1

    def stop_workers(self) -> None:
        self.stop_calls += 1


class _FakeWizard:
    callback: Any
    accepted: ClassVar[bool] = True
    events: ClassVar[list[str]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.callback = kwargs["on_root_confirmed"]
        self.selected_root: Path | None = None
        self.__class__.events.append("wizard")

    def exec(self) -> int:
        if self.accepted:
            self.__class__.events.append("confirm")
            self.selected_root = Path("/attached/mail-dock")
            self.callback(self.selected_root)
            return app.QWizardAccepted
        self.__class__.events.append("cancel")
        return 0


class _FakeWindow:
    def __init__(self) -> None:
        self.stop_calls = 0

    def stop_workers(self) -> None:
        self.stop_calls += 1


class _FakeThread:
    def isRunning(self) -> bool:  # noqa: N802
        return False


_APPLICATION = _FakeApplication()


def test_run_gui_starts_session_only_after_root_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeSession.instances.clear()
    _FakeContext.instances.clear()
    _FakeWizard.events.clear()
    window = _FakeWindow()

    monkeypatch.setattr(app, "QApplication", _FakeApplication)
    monkeypatch.setattr(app, "register_schemes", lambda: None)
    monkeypatch.setattr(app, "_available_root", lambda _settings, _requested: None)
    monkeypatch.setattr(app, "StorageSession", _FakeSession)
    monkeypatch.setattr(app, "AppContext", _FakeContext)
    monkeypatch.setattr(app, "SetupWizard", _FakeWizard)
    monkeypatch.setattr(
        app,
        "_start_verification",
        lambda _application, _session, _context: (_FakeThread(), {"error": None, "window": window}),
    )

    assert app.run_gui(config.AppConfig()) == 0
    assert _FakeWizard.events == ["wizard", "confirm"]
    assert len(_FakeSession.instances) == 1
    assert _FakeSession.instances[0].enter_calls == 1
    assert _FakeSession.instances[0].exit_calls == 1
    assert _FakeContext.instances[0].save_calls == 1
    assert window.stop_calls == 1


def test_run_gui_cancelled_wizard_does_not_create_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeSession.instances.clear()
    _FakeWizard.events.clear()
    _FakeWizard.accepted = False

    monkeypatch.setattr(app, "QApplication", _FakeApplication)
    monkeypatch.setattr(app, "register_schemes", lambda: None)
    monkeypatch.setattr(app, "_available_root", lambda _settings, _requested: None)
    monkeypatch.setattr(app, "StorageSession", _FakeSession)
    monkeypatch.setattr(app, "SetupWizard", _FakeWizard)

    try:
        assert app.run_gui(config.AppConfig()) == 0
    finally:
        _FakeWizard.accepted = True

    assert _FakeWizard.events == ["wizard", "cancel"]
    assert _FakeSession.instances == []


def test_run_gui_releases_a_partially_started_session_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeSession.instances.clear()
    _FakeSession.fail_enter = True
    _FakeWizard.events.clear()
    errors: list[BaseException] = []

    monkeypatch.setattr(app, "QApplication", _FakeApplication)
    monkeypatch.setattr(app, "register_schemes", lambda: None)
    monkeypatch.setattr(app, "_available_root", lambda _settings, _requested: None)
    monkeypatch.setattr(app, "StorageSession", _FakeSession)
    monkeypatch.setattr(app, "SetupWizard", _FakeWizard)
    monkeypatch.setattr(app, "_show_error", errors.append)

    try:
        assert app.run_gui(config.AppConfig()) == 1
    finally:
        _FakeSession.fail_enter = False

    assert len(_FakeSession.instances) == 1
    assert _FakeSession.instances[0].enter_calls == 1
    assert _FakeSession.instances[0].exit_calls == 1
    assert len(errors) == 1


def test_run_gui_acknowledges_unsupported_existing_root_once_and_recreates_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeSession.instances.clear()
    _FakeSession.unsupported_remaining = 1
    reloaded_settings = config.AppConfig(sync_on_startup=False)
    acknowledged: list[StorageUnsupportedError] = []
    window = _FakeWindow()

    monkeypatch.setattr(app, "QApplication", _FakeApplication)
    monkeypatch.setattr(app, "register_schemes", lambda: None)
    monkeypatch.setattr(app, "_available_root", lambda _settings, _requested: Path("/attached"))
    monkeypatch.setattr(app, "StorageSession", _FakeSession)
    monkeypatch.setattr(app, "AppContext", _FakeContext)
    monkeypatch.setattr(
        app,
        "_confirm_storage_unsupported",
        lambda error: acknowledged.append(error) or True,
    )
    monkeypatch.setattr(
        app,
        "_acknowledge_storage_unsupported",
        lambda _settings, error: reloaded_settings,
    )
    monkeypatch.setattr(
        app,
        "_start_verification",
        lambda _application, _session, _context: (_FakeThread(), {"error": None, "window": window}),
    )

    assert app.run_gui(config.AppConfig()) == 0

    assert len(acknowledged) == 1
    assert len(_FakeSession.instances) == 2
    assert _FakeSession.instances[1].settings is reloaded_settings
    assert _FakeSession.instances[1].exit_calls == 1


def test_run_gui_rejects_unsupported_existing_root_with_exit_code_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeSession.instances.clear()
    _FakeSession.unsupported_remaining = 1

    monkeypatch.setattr(app, "QApplication", _FakeApplication)
    monkeypatch.setattr(app, "register_schemes", lambda: None)
    monkeypatch.setattr(app, "_available_root", lambda _settings, _requested: Path("/attached"))
    monkeypatch.setattr(app, "StorageSession", _FakeSession)
    monkeypatch.setattr(app, "_confirm_storage_unsupported", lambda _error: False)
    monkeypatch.setattr(
        app,
        "_start_verification",
        lambda *_args: pytest.fail("verification must not start after rejection"),
    )

    try:
        assert app.run_gui(config.AppConfig()) == 3
    finally:
        _FakeSession.unsupported_remaining = 0

    assert len(_FakeSession.instances) == 1


def test_acknowledge_storage_unsupported_persists_timestamp_and_reloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = config.AppConfig(
        storage_profiles={
            "root-uuid": {
                "capability_level": "unsupported",
                "capabilities": {},
                "checked_path": "/attached",
                "storage_fingerprint": "posix:1:/attached",
            }
        }
    )
    error = StorageUnsupportedError("root-uuid", "unsupported")
    saved: list[config.AppConfig] = []
    reloaded = config.AppConfig(sync_on_startup=False)
    monkeypatch.setattr(app.config, "save", saved.append)
    monkeypatch.setattr(app.config, "load", lambda: reloaded)

    assert app._acknowledge_storage_unsupported(settings, error) is reloaded

    assert len(saved) == 1
    acknowledged = saved[0].storage_profiles["root-uuid"]
    assert isinstance(acknowledged, dict)
    assert isinstance(acknowledged["capability_ack_at"], str)
