from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest
from PySide6.QtWidgets import QFileDialog

from mail_dock import config
from mail_dock.domain.fetcher import CancelToken
from mail_dock.presentation import app, strings
from mail_dock.presentation.views.main_window import MainWindow

pytestmark = pytest.mark.gui


class _WindowContext:
    def __init__(self, root: Path, events: list[str]) -> None:
        self.storage_root = root
        self.root_uuid = "old-root"
        self.settings = config.AppConfig(storage_root_uuid=self.root_uuid)
        self.capability_level = "ok"
        self.encryption_declaration = "encrypted"
        self.credential_storage = "keyring"
        self.storage_root_switch_handler: Any = None
        self.storage_setup_handler: Any = None
        self.window_created_handler: Any = None
        self._events = events

    def stop_workers(self) -> None:
        self._events.append("stop-workers")

    def save_settings(self, settings: config.AppConfig) -> None:
        self.settings = settings


class _Session:
    created: ClassVar[list[_Session]] = []

    def __init__(
        self,
        settings: config.AppConfig,
        root: Path,
        events: list[str] | None = None,
    ) -> None:
        self.settings = settings
        self.root = root
        self.root_uuid = "new-root" if root.name == "new" else "old-root"
        self._events = events if events is not None else []
        self.__class__.created.append(self)

    def __enter__(self) -> _Session:
        self._events.append("new-enter")
        return self

    def __exit__(self, *_args: object) -> None:
        self._events.append("old-exit")


def test_switch_releases_old_session_before_starting_new_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    old_session = _Session(config.AppConfig(), old_root, events)
    old_context = _WindowContext(old_root, events)
    runtime = app._GuiRuntime(cast(Any, object()), config.AppConfig())
    runtime.attach(cast(Any, old_session), cast(Any, old_context))

    class _ReplacementSession(_Session):
        def __enter__(self) -> _ReplacementSession:
            events.append("new-enter")
            return self

        def __exit__(self, *_args: object) -> None:
            events.append("new-exit")

    monkeypatch.setattr(app, "StorageSession", _ReplacementSession)
    monkeypatch.setattr(
        app,
        "AppContext",
        lambda session, _settings: _WindowContext(session.root, events),
    )
    monkeypatch.setattr(
        app,
        "initialize_root",
        lambda _root: SimpleNamespace(root_uuid="new-root"),
    )
    shown: list[str] = []
    cast(Any, runtime).verify_and_show = lambda: shown.append("shown")

    runtime._replace_with_root(new_root)

    assert events == ["stop-workers", "old-exit", "new-enter"]
    assert runtime.context is not None
    assert runtime.context.storage_root == new_root
    assert shown == ["shown"]


def test_cancelled_setup_keeps_current_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    root = tmp_path / "old"
    session = _Session(config.AppConfig(), root)
    context = _WindowContext(root, events)
    runtime = app._GuiRuntime(cast(Any, object()), config.AppConfig())
    runtime.attach(cast(Any, session), cast(Any, context))

    class _Wizard:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def exec(self) -> int:
            return 0

    monkeypatch.setattr(app, "SetupWizard", _Wizard)
    runtime.start_setup()

    assert cast(Any, runtime.session) is session
    assert cast(Any, runtime.context) is context
    assert events == []


def test_switch_warning_cancel_does_not_call_runtime(
    qtbot: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.gui.test_main_window import _Context

    calls: list[Path] = []
    window = MainWindow(
        cast(Any, _Context()),
        on_storage_root_switch=calls.append,
    )
    qtbot.addWidget(window)
    window._sync_token = CancelToken()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_args: "C:/new-root")

    class _Confirmation:
        def __init__(self, message: str, *_args: object, **_kwargs: object) -> None:
            assert message == strings.DIALOG_CONFIRM_STORAGE_SWITCH_BUSY

        def confirmed(self) -> bool:
            return False

    monkeypatch.setattr(
        "mail_dock.presentation.views.main_window.ConfirmationDialog",
        _Confirmation,
    )

    window.storage_switch_action.trigger()

    assert calls == []
    window.stop_workers()