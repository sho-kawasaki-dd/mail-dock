from __future__ import annotations

from typing import Any, cast

import pytest
from PySide6.QtGui import QStandardItemModel

from mail_dock import config
from mail_dock.presentation import strings
from mail_dock.presentation.views.dialogs.settings_dialog import AccountDialog, SettingsDialog

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


class _AccountRepository:
    def __init__(self, account: dict[str, object]) -> None:
        self.accounts = {str(account["id"]): dict(account)}

    def upsert_account(self, account: dict[str, object]) -> str:
        account_id = str(account["id"])
        self.accounts[account_id] = dict(account)
        return account_id

    def list_accounts(self) -> list[dict[str, object]]:
        return list(self.accounts.values())


class _CredentialStore:
    def __init__(self) -> None:
        self.passwords: dict[str, str] = {}

    def set_password(self, account_id: str, password: str) -> None:
        self.passwords[account_id] = password

    def get_password(self, account_id: str) -> str | None:
        return self.passwords.get(account_id)

    def delete_password(self, account_id: str) -> None:
        self.passwords.pop(account_id, None)


class _ManifestWriter:
    def append(self, _event: object) -> None:
        return None

    def flush_and_sync(self) -> None:
        return None

    def close(self) -> None:
        return None


class _ManifestReader:
    def read_all_events(self) -> tuple[object, ...]:
        return ()


class _AccountEditContext:
    def __init__(self, repository: _AccountRepository, credential_store: _CredentialStore) -> None:
        self.connection_manager = None
        self.credential_store = credential_store
        self._repository = repository

    def create_message_repository(self) -> _AccountRepository:
        return self._repository

    def create_manifest_writer(self, _account_id: str) -> _ManifestWriter:
        return _ManifestWriter()

    def create_manifest_reader(self, _account_id: str) -> _ManifestReader:
        return _ManifestReader()


def _editable_account() -> dict[str, object]:
    return {
        "id": "account-1",
        "provider_type": "onamae_imap",
        "display_name": None,
        "host": "imap.example.com",
        "port": 993,
        "username": "user",
        "is_enabled": 1,
    }


def test_account_dialog_edit_locks_account_id_and_prefills_fields(qtbot: Any) -> None:
    account = _editable_account()
    repository = _AccountRepository(account)
    credentials = _CredentialStore()
    dialog = AccountDialog(_AccountEditContext(repository, credentials), account=account)
    qtbot.addWidget(dialog)

    assert dialog._account_id_edit.text() == "account-1"
    assert dialog._account_id_edit.isReadOnly()
    assert dialog._host_edit.text() == "imap.example.com"
    assert dialog._username_edit.text() == "user"
    dialog._stop_worker()


def test_account_dialog_edit_saves_display_name_without_connection_test(qtbot: Any) -> None:
    account = _editable_account()
    repository = _AccountRepository(account)
    credentials = _CredentialStore()
    credentials.passwords["account-1"] = "secret"
    dialog = AccountDialog(_AccountEditContext(repository, credentials), account=account)
    qtbot.addWidget(dialog)

    dialog._display_name_edit.setText("\u4ed5\u4e8b")
    with qtbot.waitSignal(dialog.account_updated, timeout=2_000):
        dialog._save_account()

    assert repository.accounts["account-1"]["display_name"] == "\u4ed5\u4e8b"
    assert credentials.passwords["account-1"] == "secret"
    dialog._stop_worker()


def test_account_dialog_edit_requires_connection_test_when_host_changes(qtbot: Any) -> None:
    account = _editable_account()
    repository = _AccountRepository(account)
    credentials = _CredentialStore()
    dialog = AccountDialog(_AccountEditContext(repository, credentials), account=account)
    qtbot.addWidget(dialog)

    dialog._host_edit.setText("imap.other-example.com")
    dialog._save_account()

    assert dialog._status_label.text() == strings.SETTINGS_STATUS_CONNECTION_REQUIRED
    assert repository.accounts["account-1"]["host"] == "imap.example.com"
    dialog._stop_worker()


def test_active_settings_are_saved_with_purge_controls(qtbot: Any) -> None:
    context = _Context()
    dialog = SettingsDialog(context)
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: not dialog._account_list.isEnabled(), timeout=2_000)
    qtbot.waitUntil(lambda: dialog._account_list.isEnabled(), timeout=2_000)

    dialog._max_message_size.setValue(12)
    dialog._block_remote_images.setChecked(False)
    dialog._sync_on_startup.setChecked(False)
    dialog._sync_interval_minutes.setValue(15)
    dialog._startup_verification.setCurrentIndex(dialog._startup_verification.findData("full"))
    dialog._purge_mode.setCurrentIndex(dialog._purge_mode.findData("immediate"))
    dialog._trash_grace_days.setValue(45)
    dialog._remote_delete_mode.setCurrentIndex(dialog._remote_delete_mode.findData("expunge"))
    dialog._remote_trash_folder.setText("INBOX.Trash")
    dialog._delete_batch_limit.setValue(250)
    dialog._heartbeat_interval_sec.setValue(7)
    dialog._sync_log_retention_days.setValue(120)
    dialog._db_backup_to_local_disk.setChecked(True)

    assert dialog._save_settings()
    assert context.saved[-1].max_message_bytes == 12 * 1024 * 1024
    assert not context.saved[-1].block_remote_images
    assert not context.saved[-1].sync_on_startup
    assert context.saved[-1].sync_interval_minutes == 15
    assert context.saved[-1].startup_verification == "full"
    assert context.saved[-1].purge_mode == "immediate"
    assert context.saved[-1].trash_grace_days == 45
    assert context.saved[-1].remote_delete_mode == "expunge"
    assert context.saved[-1].remote_trash_folder == "INBOX.Trash"
    assert context.saved[-1].delete_batch_limit == 250
    assert context.saved[-1].heartbeat_interval_sec == 7
    assert context.saved[-1].sync_log_retention_days == 120
    assert context.saved[-1].db_backup_to_local_disk
    assert dialog._purge_mode_warning.text() == strings.SETTINGS_WARNING_PURGE_IMMEDIATE
    assert dialog._db_backup_warning.text() == strings.SETTINGS_WARNING_DB_BACKUP_TO_LOCAL_DISK
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


def test_account_added_and_updated_emit_accounts_changed(qtbot: Any) -> None:
    context = _Context()
    dialog = SettingsDialog(context)
    qtbot.addWidget(dialog)

    with qtbot.waitSignal(dialog.accounts_changed, timeout=2_000):
        dialog._account_added("account-1")
    with qtbot.waitSignal(dialog.accounts_changed, timeout=2_000):
        dialog._account_updated("account-1")
    dialog._stop_worker()


def test_storage_reprobe_button_uses_injected_context_callback(qtbot: Any) -> None:
    context = _Context()
    dialog = SettingsDialog(context)
    qtbot.addWidget(dialog)

    dialog._reprobe_storage()

    assert context.reprobe_calls == 1
    assert "DEGRADED" in dialog._capability_label.text()
    dialog._stop_worker()


def test_remote_trash_detection_status_is_displayed(qtbot: Any) -> None:
    dialog = SettingsDialog(_Context())
    qtbot.addWidget(dialog)

    dialog._set_remote_trash_status("INBOX.Trash")

    expected = strings.SETTINGS_STATUS_REMOTE_TRASH_DETECTED.format(folder="INBOX.Trash")
    assert dialog._remote_trash_status.text() == expected
    dialog._stop_worker()
