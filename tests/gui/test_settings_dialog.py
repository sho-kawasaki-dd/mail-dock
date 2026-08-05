from __future__ import annotations

from typing import Any, cast

import pytest
from PySide6.QtGui import QStandardItemModel

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
        self.root_uuid = "root-1"
        self.settings = config.AppConfig(
            storage_root_uuid=self.root_uuid,
            storage_profiles={
                self.root_uuid: {
                    "capability_level": "ok",
                    "encryption": "unknown",
                }
            },
        )
        self.saved: list[config.AppConfig] = []
        self.credential_storage = "keyring"
        self.keyring_supported = True
        self.credential_backend_name = "test.backend"
        self.reprobe_calls = 0

    def create_message_repository(self) -> _Repository:
        return _Repository()

    def save_settings(self, settings: config.AppConfig) -> None:
        self.saved.append(settings)

    def reprobe_storage(self) -> dict[str, object]:
        self.reprobe_calls += 1
        return {"capability_level": "degraded"}


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


def test_storage_declaration_and_credential_mode_are_saved(qtbot: Any) -> None:
    context = _Context()
    dialog = SettingsDialog(context)
    qtbot.addWidget(dialog)

    dialog._encryption_combo.setCurrentIndex(dialog._encryption_combo.findData("unencrypted"))
    dialog._credential_storage_combo.setCurrentIndex(
        dialog._credential_storage_combo.findData("session_only")
    )

    assert dialog._save_settings()
    saved = context.saved[-1]
    profile = saved.storage_profiles["root-1"]
    assert isinstance(profile, dict)
    assert profile["encryption"] == "unencrypted"
    assert saved.credential_storage == "session_only"
    dialog._stop_worker()


def test_keyring_is_disabled_when_backend_is_unsupported(qtbot: Any) -> None:
    context = _Context()
    context.keyring_supported = False
    context.credential_storage = "session_only"
    dialog = SettingsDialog(context)
    qtbot.addWidget(dialog)

    keyring_index = dialog._credential_storage_combo.findData("keyring")
    model = cast(QStandardItemModel, dialog._credential_storage_combo.model())
    keyring_item = model.item(keyring_index)
    assert keyring_item is not None
    assert not keyring_item.isEnabled()
    assert dialog._credential_storage_combo.currentData() == "session_only"
    dialog._stop_worker()


def test_storage_reprobe_button_uses_injected_context_callback(qtbot: Any) -> None:
    context = _Context()
    dialog = SettingsDialog(context)
    qtbot.addWidget(dialog)

    dialog._reprobe_storage()

    assert context.reprobe_calls == 1
    assert "DEGRADED" in dialog._capability_label.text()
    dialog._stop_worker()
