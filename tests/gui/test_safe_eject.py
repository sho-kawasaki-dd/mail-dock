from __future__ import annotations

from typing import Any, cast

import pytest

from mail_dock import config
from mail_dock.presentation import app, strings
from mail_dock.presentation.views.setup_wizard import SetupWizard

pytestmark = pytest.mark.gui


class _Session:
    def __init__(self, events: list[str]) -> None:
        self.settings = config.AppConfig()
        self.events = events

    def checkpoint_for_detach(self) -> None:
        self.events.append("checkpoint")

    def __exit__(self, *_args: object) -> None:
        self.events.append("session_exit")


class _DetailView:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def close(self) -> None:
        self.events.append("detail_close")


class _Window:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.detail_view = _DetailView(events)

    def stop_workers(self) -> None:
        self.events.append("stop_workers")


class _Monitor:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def stop(self) -> None:
        self.events.append("monitor_stop")

    def mark_detached_by_user(self) -> None:
        self.events.append("mark_detached_by_user")


def test_safe_detach_releases_resources_in_required_order() -> None:
    events: list[str] = []
    runtime = app._GuiRuntime(None, config.AppConfig())  # type: ignore[arg-type]
    runtime.session = _Session(events)  # type: ignore[assignment]
    runtime.context = cast(Any, object())
    runtime.window = _Window(events)
    runtime.storage_monitor = _Monitor(events)  # type: ignore[assignment]

    runtime.safe_detach()

    assert events == [
        "stop_workers",
        "monitor_stop",
        "checkpoint",
        "detail_close",
        "session_exit",
        "mark_detached_by_user",
    ]
    assert runtime.session is None
    assert runtime.context is None
    assert runtime.window is not None


def test_setup_wizard_shows_storage_removal_guidance(qtbot: Any) -> None:
    wizard = SetupWizard()
    qtbot.addWidget(wizard)

    labels = [label.text() for label in wizard.findChildren(type(wizard._root_status))]

    assert strings.WIZARD_STORAGE_REMOVAL_GUIDANCE in labels
