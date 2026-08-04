from __future__ import annotations

from typing import Any

import pytest

from mail_dock import config
from mail_dock.presentation.views.dialogs.settings_dialog import SettingsDialog

pytestmark = pytest.mark.gui


class _Repository:
    def list_accounts(self) -> list[dict[str, object]]:
        return []

    def list_folders(self, _account_id: str) -> list[dict[str, object]]:
        return []


class _Context:
    def __init__(self) -> None:
        self.settings = config.AppConfig()
        self.saved: list[config.AppConfig] = []

    def create_message_repository(self) -> _Repository:
        return _Repository()

    def save_settings(self, settings: config.AppConfig) -> None:
        self.saved.append(settings)


def test_active_settings_are_saved_and_phase4_controls_are_absent(qtbot: Any) -> None:
    context = _Context()
    dialog = SettingsDialog(context)
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: not dialog._account_list.isEnabled(), timeout=2_000)
    qtbot.waitUntil(lambda: dialog._account_list.isEnabled(), timeout=2_000)

    dialog._max_message_size.setValue(12)
    dialog._block_remote_images.setChecked(False)
    dialog._sync_on_startup.setChecked(False)
    dialog._startup_verification.setCurrentIndex(dialog._startup_verification.findData("full"))

    assert dialog._save_settings()
    assert context.saved[-1].max_message_bytes == 12 * 1024 * 1024
    assert not context.saved[-1].block_remote_images
    assert not context.saved[-1].sync_on_startup
    assert context.saved[-1].startup_verification == "full"
    assert not hasattr(dialog, "_remote_delete_mode")
    assert not hasattr(dialog, "_purge_mode")
    dialog._stop_worker()
