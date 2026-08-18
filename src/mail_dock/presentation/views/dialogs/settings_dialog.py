"""Application settings and account management dialog."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QStandardItemModel
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from mail_dock import config
from mail_dock.domain.accounts import validate_account_id
from mail_dock.domain.errors import MailDockError
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.repository import MessageRecord
from mail_dock.presentation import strings
from mail_dock.presentation.errors import present_error
from mail_dock.presentation.threads.worker import Worker
from mail_dock.usecases.register_account import (
    load_credentials,
    register_account,
    update_account,
)
from mail_dock.usecases.sync_folders import refresh_folders, set_sync_target

from .progress_dialog import ProgressDialog

_MEGABYTE = 1024 * 1024


class _OperationResult:
    def __init__(self, operation: str, value: object) -> None:
        self.operation = operation
        self.value = value


class AccountDialog(QDialog):
    """Collect a new account, or edit an existing one, without exposing its password to SQLite."""

    account_added = Signal(str)
    account_updated = Signal(str)

    def __init__(
        self,
        context: Any,
        parent: QWidget | None = None,
        account: MessageRecord | None = None,
    ) -> None:
        super().__init__(parent)
        self._context = context
        self._existing_account = account
        self._editing = account is not None
        self._worker = Worker(getattr(context, "connection_manager", None))
        self._worker.result.connect(self._operation_succeeded)
        self._worker.failed.connect(self._operation_failed)
        self._worker.cancelled.connect(self._operation_cancelled)
        self._worker.start()
        self._operation: str | None = None
        self._progress_dialog: ProgressDialog | None = None
        self._connection_test_passed = False
        self._build_ui()

    def _build_ui(self) -> None:
        account = self._existing_account
        self.setWindowTitle(
            strings.SETTINGS_BUTTON_EDIT_ACCOUNT if self._editing else strings.SETTINGS_BUTTON_ADD_ACCOUNT
        )
        layout = QVBoxLayout(self)
        form = QFormLayout()

        self._account_id_edit = QLineEdit(self)
        self._host_edit = QLineEdit(self)
        self._port_edit = QSpinBox(self)
        self._port_edit.setRange(1, 65535)
        self._port_edit.setValue(993)
        self._username_edit = QLineEdit(self)
        self._password_edit = QLineEdit(self)
        self._password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._display_name_edit = QLineEdit(self)
        self._status_label = QLabel(self)
        self._status_label.setWordWrap(True)

        if account is not None:
            self._account_id_edit.setText(str(account.get("id", "")))
            self._account_id_edit.setReadOnly(True)
            self._account_id_edit.setToolTip(strings.SETTINGS_HINT_ACCOUNT_ID_LOCKED)
            self._host_edit.setText(str(account.get("host", "")))
            self._port_edit.setValue(int(account.get("port") or 993))
            self._username_edit.setText(str(account.get("username", "")))
            self._display_name_edit.setText(str(account.get("display_name") or ""))
            self._password_edit.setPlaceholderText(strings.SETTINGS_HINT_PASSWORD_UNCHANGED)
        # Captured after prefilling so unrelated display-name-only edits skip the connection test.
        self._original_connection_values = (
            self._host_edit.text().strip(),
            self._port_edit.value(),
            self._username_edit.text().strip(),
        )

        for field in (
            self._account_id_edit,
            self._host_edit,
            self._username_edit,
            self._password_edit,
        ):
            field.textChanged.connect(self._invalidate_connection_test)
        self._port_edit.valueChanged.connect(self._invalidate_connection_test)
        form.addRow(strings.SETTINGS_LABEL_ACCOUNT_ID, self._account_id_edit)
        form.addRow(strings.SETTINGS_LABEL_HOST, self._host_edit)
        form.addRow(strings.SETTINGS_LABEL_PORT, self._port_edit)
        form.addRow(strings.SETTINGS_LABEL_USERNAME, self._username_edit)
        form.addRow(strings.SETTINGS_LABEL_PASSWORD, self._password_edit)
        form.addRow(strings.SETTINGS_LABEL_DISPLAY_NAME, self._display_name_edit)
        layout.addLayout(form)

        self._test_button = QPushButton(strings.SETTINGS_BUTTON_TEST_CONNECTION, self)
        self._test_button.clicked.connect(self._test_connection)
        layout.addWidget(self._test_button)
        layout.addWidget(self._status_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self._save_account)
        buttons.rejected.connect(self.reject)
        self._buttons = buttons
        layout.addWidget(buttons)

    def _invalidate_connection_test(self, *_args: object) -> None:
        self._connection_test_passed = False

    def _connection_fields_changed(self) -> bool:
        """Whether host/port/username/password differ from the stored account.

        Editing only the display name should not force a connection re-test.
        """

        if not self._editing:
            return True
        if self._password_edit.text():
            return True
        current = (
            self._host_edit.text().strip(),
            self._port_edit.value(),
            self._username_edit.text().strip(),
        )
        return current != self._original_connection_values

    def _resolved_test_password(self, values: dict[str, Any]) -> str:
        if values["password"]:
            return cast(str, values["password"])
        return load_credentials(self._context.credential_store, values["account_id"])

    def _test_connection(self) -> None:
        values = self._account_values()
        if values is None:
            self._status_label.setText(strings.SETTINGS_STATUS_ACCOUNT_REQUIRED)
            return
        try:
            validate_account_id(values["account_id"])
        except Exception as error:
            self._show_error(error)
            return

        self._connection_test_passed = False
        self._test_button.setEnabled(False)
        self._status_label.setText(strings.SETTINGS_STATUS_TESTING_CONNECTION)
        token = self._submit(
            "connection",
            lambda: _test_connection(
                self._context, {**values, "password": self._resolved_test_password(values)}
            ),
        )
        self._show_progress(strings.SETTINGS_STATUS_TESTING_CONNECTION, token)

    def _save_account(self) -> None:
        values = self._account_values()
        if values is None:
            self._status_label.setText(strings.SETTINGS_STATUS_ACCOUNT_REQUIRED)
            return
        if self._connection_fields_changed() and not self._connection_test_passed:
            self._status_label.setText(strings.SETTINGS_STATUS_CONNECTION_REQUIRED)
            return
        self._buttons.setEnabled(False)
        if self._editing:
            existing = self._existing_account or {}
            is_enabled = bool(existing.get("is_enabled", 1))
            self._submit(
                "update",
                lambda: update_account(
                    self._context.create_message_repository(),
                    self._context.credential_store,
                    account_id=values["account_id"],
                    host=values["host"],
                    port=values["port"],
                    username=values["username"],
                    password=values["password"] or None,
                    display_name=values["display_name"] or None,
                    is_enabled=is_enabled,
                ),
            )
        else:
            self._submit(
                "register",
                lambda: register_account(
                    self._context.create_message_repository(),
                    self._context.credential_store,
                    account_id=values["account_id"],
                    host=values["host"],
                    port=values["port"],
                    username=values["username"],
                    password=values["password"],
                    display_name=values["display_name"] or None,
                ),
            )

    def _account_values(self) -> dict[str, Any] | None:
        account_id = self._account_id_edit.text().strip()
        host = self._host_edit.text().strip()
        username = self._username_edit.text().strip()
        password = self._password_edit.text()
        if not account_id or not host or not username:
            return None
        if not self._editing and not password:
            return None
        return {
            "account_id": account_id,
            "host": host,
            "port": self._port_edit.value(),
            "username": username,
            "password": password,
            "display_name": self._display_name_edit.text().strip(),
        }

    def _submit(self, operation: str, callback: Callable[[], object]) -> CancelToken:
        self._worker.cancel_all()
        self._operation = operation
        return self._worker.submit(lambda: _OperationResult(operation, callback()))

    def _show_progress(self, message: str, token: CancelToken) -> None:
        self._close_progress()
        dialog = ProgressDialog(message, self)
        dialog.attach_token(token)
        self._progress_dialog = dialog
        dialog.show()

    def _operation_succeeded(self, value: object) -> None:
        if not isinstance(value, _OperationResult) or value.operation != self._operation:
            return
        self._operation = None
        self._close_progress()
        if value.operation == "connection":
            self._connection_test_passed = True
            self._test_button.setEnabled(True)
            self._status_label.setText(strings.SETTINGS_STATUS_CONNECTION_OK)
        elif value.operation == "register":
            account_id = value.value
            if isinstance(account_id, str):
                self.account_added.emit(account_id)
                self._stop_worker()
                super().accept()
        elif value.operation == "update":
            account_id = value.value
            if isinstance(account_id, str):
                self.account_updated.emit(account_id)
                self._stop_worker()
                super().accept()

    def _operation_failed(self, error: object) -> None:
        if self._operation is None:
            return
        self._operation = None
        self._close_progress()
        self._test_button.setEnabled(True)
        self._buttons.setEnabled(True)
        self._show_error(
            error if isinstance(error, BaseException) else MailDockError("account operation failed")
        )

    def _operation_cancelled(self) -> None:
        self._operation = None
        self._close_progress()
        self._test_button.setEnabled(True)
        self._buttons.setEnabled(True)

    def _show_error(self, error: BaseException) -> None:
        self._status_label.setText(present_error(error).message)

    def reject(self) -> None:
        self._close_progress()
        self._stop_worker()
        super().reject()

    def _stop_worker(self) -> None:
        self._worker.stop()

    def _close_progress(self) -> None:
        if self._progress_dialog is not None:
            self._progress_dialog.finish()
            self._progress_dialog.deleteLater()
            self._progress_dialog = None


class SettingsDialog(QDialog):
    """Edit active settings and account synchronization targets.

    The dialog only talks to use cases and ``AppContext`` factories. Database,
    credential-store, and IMAP objects are created inside the dialog worker's
    thread and never cross into the UI thread.
    """

    settings_saved = Signal(object)
    accounts_changed = Signal()

    def __init__(self, context: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._context = context
        self._settings: config.AppConfig = context.settings
        self._worker = Worker(getattr(context, "connection_manager", None))
        self._worker.result.connect(self._operation_succeeded)
        self._worker.failed.connect(self._operation_failed)
        self._worker.cancelled.connect(self._operation_cancelled)
        self._worker.start()
        self._operation: str | None = None
        self._progress_dialog: ProgressDialog | None = None
        self._accounts: tuple[MessageRecord, ...] = ()
        self._folders: tuple[MessageRecord, ...] = ()
        self._selected_account_id: str | None = None
        self._keyring_supported = bool(getattr(context, "keyring_supported", True))
        self._build_ui()
        self._load_accounts()

    def _active_storage_profile(self) -> dict[str, config.JSONValue]:
        root_uuid = getattr(self._context, "root_uuid", None) or self._settings.storage_root_uuid
        raw_profile = (
            self._settings.storage_profiles.get(root_uuid) if isinstance(root_uuid, str) else None
        )
        return dict(raw_profile) if isinstance(raw_profile, dict) else {}

    def _encryption_declaration(self) -> str:
        value = self._encryption_combo.currentData()
        return value if isinstance(value, str) else "unknown"

    def _credential_storage_mode(self) -> str:
        value = self._credential_storage_combo.currentData()
        return value if isinstance(value, str) else "session_only"

    def _set_capability_label(self, level: object) -> None:
        capability = level.lower() if isinstance(level, str) else "unknown"
        if capability == "ok":
            display = "OK"
        elif capability == "degraded":
            display = "DEGRADED (制限あり)"
        elif capability == "unsupported":
            display = "UNSUPPORTED (非対応)"
        else:
            display = capability
        self._capability_label.setText(f"{strings.SETTINGS_LABEL_STORAGE_CAPABILITY}: {display}")

    def _update_keyring_help(self) -> None:
        self._keyring_backend_label.setText(
            f"{strings.SETTINGS_LABEL_KEYRING_BACKEND}: "
            f"{getattr(self._context, 'credential_backend_name', 'unknown')}"
        )
        if self._keyring_supported:
            self._credential_storage_help.setText("")
        else:
            self._credential_storage_help.setText(
                strings.SETTINGS_WARNING_KEYRING_BACKEND_UNSUPPORTED
            )

    def _reprobe_storage(self) -> None:
        reprobe = getattr(self._context, "reprobe_storage", None)
        if not callable(reprobe):
            self._folder_status.setText(strings.SETTINGS_STATUS_REPROBE_UNAVAILABLE)
            return
        self._reprobe_button.setEnabled(False)
        self._capability_label.setText(strings.SETTINGS_STATUS_REPROBING_STORAGE)
        try:
            result = reprobe()
        except Exception as error:
            self._reprobe_button.setEnabled(True)
            self._capability_label.setText(present_error(error).message)
            return
        self._reprobe_button.setEnabled(True)
        if isinstance(result, Mapping):
            self._set_capability_label(result.get("capability_level"))
        self._settings = getattr(self._context, "settings", self._settings)
        self._folder_status.setText(strings.SETTINGS_STATUS_REPROBE_COMPLETE)

    def _build_ui(self) -> None:
        self.setWindowTitle(strings.SETTINGS_TITLE)
        layout = QVBoxLayout(self)

        settings_group = QGroupBox(strings.SETTINGS_TITLE, self)
        settings_form = QFormLayout(settings_group)
        self._max_message_size = QSpinBox(settings_group)
        self._max_message_size.setObjectName("maxMessageSizeSpinBox")
        self._max_message_size.setRange(1, 1_048_576)
        self._max_message_size.setSuffix(" MB")
        self._max_message_size.setValue(max(1, round(self._settings.max_message_bytes / _MEGABYTE)))
        self._block_remote_images = QCheckBox(settings_group)
        self._block_remote_images.setObjectName("blockRemoteImagesCheckBox")
        self._block_remote_images.setChecked(self._settings.block_remote_images)
        self._sync_on_startup = QCheckBox(settings_group)
        self._sync_on_startup.setObjectName("syncOnStartupCheckBox")
        self._sync_on_startup.setChecked(self._settings.sync_on_startup)
        self._startup_verification = QComboBox(settings_group)
        self._startup_verification.setObjectName("startupVerificationComboBox")
        self._startup_verification.addItem(strings.SETTINGS_VERIFICATION_QUICK, "quick")
        self._startup_verification.addItem(strings.SETTINGS_VERIFICATION_FULL, "full")
        index = self._startup_verification.findData(self._settings.startup_verification)
        self._startup_verification.setCurrentIndex(max(0, index))
        self._encryption_combo = QComboBox(settings_group)
        self._encryption_combo.setObjectName("encryptionDeclarationComboBox")
        self._encryption_combo.addItem(strings.WIZARD_ENCRYPTION_ENCRYPTED, "encrypted")
        self._encryption_combo.addItem(strings.WIZARD_ENCRYPTION_UNENCRYPTED, "unencrypted")
        self._encryption_combo.addItem(strings.WIZARD_ENCRYPTION_UNKNOWN, "unknown")
        encryption = self._active_storage_profile().get(
            "encryption",
            getattr(self._context, "encryption_declaration", "unknown"),
        )
        encryption_index = self._encryption_combo.findData(encryption)
        self._encryption_combo.setCurrentIndex(max(0, encryption_index))

        self._capability_label = QLabel(settings_group)
        self._capability_label.setObjectName("storageCapabilityLabel")
        self._set_capability_label(self._active_storage_profile().get("capability_level"))
        self._reprobe_button = QPushButton(strings.SETTINGS_BUTTON_REPROBE_STORAGE, settings_group)
        self._reprobe_button.setObjectName("reprobeStorageButton")
        self._reprobe_button.clicked.connect(self._reprobe_storage)

        self._credential_storage_combo = QComboBox(settings_group)
        self._credential_storage_combo.setObjectName("credentialStorageComboBox")
        self._credential_storage_combo.addItem(
            strings.SETTINGS_CREDENTIAL_STORAGE_KEYRING,
            "keyring",
        )
        self._credential_storage_combo.addItem(
            strings.SETTINGS_CREDENTIAL_STORAGE_SESSION_ONLY,
            "session_only",
        )
        if not self._keyring_supported:
            keyring_index = self._credential_storage_combo.findData("keyring")
            model = cast(QStandardItemModel, self._credential_storage_combo.model())
            if keyring_index >= 0:
                model.item(keyring_index).setEnabled(False)
        credential_storage = getattr(
            self._context,
            "credential_storage",
            self._settings.credential_storage,
        )
        credential_index = self._credential_storage_combo.findData(credential_storage)
        if credential_index < 0 or (
            not self._keyring_supported and credential_storage == "keyring"
        ):
            credential_index = self._credential_storage_combo.findData("session_only")
        self._credential_storage_combo.setCurrentIndex(max(0, credential_index))
        self._credential_storage_help = QLabel(settings_group)
        self._credential_storage_help.setWordWrap(True)
        self._keyring_backend_label = QLabel(settings_group)
        self._keyring_backend_label.setObjectName("keyringBackendLabel")
        self._update_keyring_help()

        settings_form.addRow(strings.SETTINGS_LABEL_MAX_MESSAGE_SIZE, self._max_message_size)
        settings_form.addRow(strings.SETTINGS_LABEL_BLOCK_REMOTE_IMAGES, self._block_remote_images)
        settings_form.addRow(strings.SETTINGS_LABEL_SYNC_ON_STARTUP, self._sync_on_startup)
        settings_form.addRow(
            strings.SETTINGS_LABEL_STARTUP_VERIFICATION,
            self._startup_verification,
        )
        settings_form.addRow(strings.SETTINGS_LABEL_ENCRYPTION, self._encryption_combo)
        capability_controls = QHBoxLayout()
        capability_controls.addWidget(self._capability_label, 1)
        capability_controls.addWidget(self._reprobe_button)
        settings_form.addRow(capability_controls)
        settings_form.addRow(
            strings.SETTINGS_LABEL_CREDENTIAL_STORAGE,
            self._credential_storage_combo,
        )
        settings_form.addRow(self._keyring_backend_label)
        settings_form.addRow(self._credential_storage_help)
        layout.addWidget(settings_group)

        accounts_group = QGroupBox(strings.SETTINGS_GROUP_ACCOUNTS, self)
        accounts_layout = QVBoxLayout(accounts_group)
        accounts_controls = QHBoxLayout()
        self._account_list = QListWidget(accounts_group)
        self._account_list.setObjectName("accountList")
        self._account_list.currentItemChanged.connect(self._account_selected)
        self._account_list.itemDoubleClicked.connect(self._edit_account)
        self._add_account_button = QPushButton(strings.SETTINGS_BUTTON_ADD_ACCOUNT, accounts_group)
        self._add_account_button.clicked.connect(self._add_account)
        self._edit_account_button = QPushButton(strings.SETTINGS_BUTTON_EDIT_ACCOUNT, accounts_group)
        self._edit_account_button.setEnabled(False)
        self._edit_account_button.clicked.connect(self._edit_account)
        accounts_controls.addWidget(self._add_account_button)
        accounts_controls.addWidget(self._edit_account_button)
        accounts_layout.addWidget(self._account_list)
        accounts_layout.addLayout(accounts_controls)
        layout.addWidget(accounts_group)

        folder_group = QGroupBox(strings.SETTINGS_LABEL_FOLDERS, self)
        folders_layout = QVBoxLayout(folder_group)
        self._folder_list = QListWidget(folder_group)
        self._folder_list.setObjectName("syncTargetFolderList")
        self._folder_status = QLabel(folder_group)
        self._folder_status.setWordWrap(True)
        folder_controls = QHBoxLayout()
        self._refresh_folders_button = QPushButton(
            strings.SETTINGS_BUTTON_REFRESH_FOLDERS,
            folder_group,
        )
        self._refresh_folders_button.clicked.connect(self._refresh_folders)
        self._save_folders_button = QPushButton(strings.SETTINGS_BUTTON_SAVE_FOLDERS, folder_group)
        self._save_folders_button.clicked.connect(self._save_folders)
        folder_controls.addWidget(self._refresh_folders_button)
        folder_controls.addWidget(self._save_folders_button)
        folders_layout.addWidget(self._folder_list)
        folders_layout.addWidget(self._folder_status)
        folders_layout.addLayout(folder_controls)
        layout.addWidget(folder_group)

        self._open_log_button = QPushButton(strings.SETTINGS_BUTTON_OPEN_LOG_FOLDER, self)
        self._open_log_button.clicked.connect(self._open_log_folder)
        layout.addWidget(self._open_log_button)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self._apply_button = buttons.addButton(
            strings.SETTINGS_BUTTON_APPLY,
            QDialogButtonBox.ButtonRole.ApplyRole,
        )
        buttons.accepted.connect(self._save_and_accept)
        buttons.rejected.connect(self.reject)
        self._ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._buttons = buttons
        layout.addWidget(buttons)

    def _load_accounts(self) -> None:
        self._set_busy(True)
        self._submit(
            "accounts",
            lambda: tuple(self._context.create_message_repository().list_accounts()),
        )

    def _account_selected(
        self,
        current: QListWidgetItem | None,
        _previous: QListWidgetItem | None,
    ) -> None:
        account_id = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
        self._selected_account_id = account_id if isinstance(account_id, str) else None
        self._edit_account_button.setEnabled(self._selected_account_id is not None)
        self._load_folders()

    def _load_folders(self) -> None:
        account_id = self._selected_account_id
        self._folder_list.clear()
        self._folders = ()
        if account_id is None:
            self._folder_status.setText(strings.SETTINGS_STATUS_NO_ACCOUNT)
            return
        self._set_busy(True)
        self._submit(
            "folders",
            lambda: tuple(self._context.create_message_repository().list_folders(account_id)),
        )

    def _refresh_folders(self) -> None:
        account_id = self._selected_account_id
        if account_id is None:
            self._folder_status.setText(strings.SETTINGS_STATUS_NO_ACCOUNT)
            return
        account = next((item for item in self._accounts if item.get("id") == account_id), None)
        if account is None:
            return
        self._set_busy(True)
        token = self._submit("folders", lambda: self._refresh_account_folders(account, account_id))
        self._show_progress(strings.SETTINGS_BUTTON_REFRESH_FOLDERS, token)

    def _refresh_account_folders(
        self,
        account: MessageRecord,
        account_id: str,
    ) -> tuple[MessageRecord, ...]:
        repository = self._context.create_message_repository()
        fetcher = self._context.create_fetcher(account)
        with fetcher:
            refresh_folders(fetcher, repository, account_id)
        return tuple(repository.list_folders(account_id))

    def _save_folders(self) -> None:
        account_id = self._selected_account_id
        if account_id is None:
            self._folder_status.setText(strings.SETTINGS_STATUS_NO_ACCOUNT)
            return
        selections = tuple(
            (
                str(folder.get("raw_name", "")),
                self._folder_list.item(index).checkState() == Qt.CheckState.Checked,
            )
            for index, folder in enumerate(self._folders)
        )
        self._set_busy(True)
        self._submit("save_folders", lambda: self._save_folder_targets(account_id, selections))

    def _save_folder_targets(
        self,
        account_id: str,
        selections: Sequence[tuple[str, bool]],
    ) -> None:
        repository = self._context.create_message_repository()
        for raw_name, enabled in selections:
            if raw_name:
                set_sync_target(repository, account_id, raw_name, enabled)

    def _add_account(self) -> None:
        dialog = AccountDialog(self._context, self)
        dialog.account_added.connect(self._account_added)
        dialog.exec()

    def _account_added(self, account_id: str) -> None:
        self._selected_account_id = account_id
        self._folder_status.setText(strings.SETTINGS_STATUS_ACCOUNT_ADDED)
        self._load_accounts()
        self.accounts_changed.emit()

    def _edit_account(self, *_args: object) -> None:
        account_id = self._selected_account_id
        account = next((item for item in self._accounts if item.get("id") == account_id), None)
        if account is None:
            return
        dialog = AccountDialog(self._context, self, account=account)
        dialog.account_updated.connect(self._account_updated)
        dialog.exec()

    def _account_updated(self, account_id: str) -> None:
        self._selected_account_id = account_id
        self._folder_status.setText(strings.SETTINGS_STATUS_ACCOUNT_UPDATED)
        self._load_accounts()
        self.accounts_changed.emit()

    def _submit(self, operation: str, callback: Callable[[], object]) -> CancelToken:
        self._worker.cancel_all()
        self._operation = operation
        return self._worker.submit(lambda: _OperationResult(operation, callback()))

    def _show_progress(self, message: str, token: CancelToken) -> None:
        self._close_progress()
        dialog = ProgressDialog(message, self)
        dialog.attach_token(token)
        self._progress_dialog = dialog
        dialog.show()

    def _operation_succeeded(self, value: object) -> None:
        if not isinstance(value, _OperationResult) or value.operation != self._operation:
            return
        self._operation = None
        self._close_progress()
        self._set_busy(False)
        if value.operation == "accounts":
            records = value.value if isinstance(value.value, tuple) else ()
            self._accounts = tuple(record for record in records if isinstance(record, dict))
            self._account_list.clear()
            selected_row = -1
            for row, account in enumerate(self._accounts):
                account_id = account.get("id")
                if not isinstance(account_id, str):
                    continue
                display_name = account.get("display_name") or account_id
                item = QListWidgetItem(f"{display_name} ({account_id})", self._account_list)
                item.setData(Qt.ItemDataRole.UserRole, account_id)
                if account_id == self._selected_account_id:
                    selected_row = row
            if self._account_list.count() and selected_row < 0:
                selected_row = 0
            if selected_row >= 0:
                self._account_list.setCurrentRow(selected_row)
            else:
                self._folder_status.setText(strings.SETTINGS_STATUS_NO_ACCOUNT)
        elif value.operation == "folders":
            records = value.value if isinstance(value.value, tuple) else ()
            self._set_folder_items(tuple(record for record in records if isinstance(record, dict)))
        elif value.operation == "save_folders":
            self._folder_status.setText(strings.SETTINGS_STATUS_FOLDER_SAVED)

    def _set_folder_items(self, records: tuple[MessageRecord, ...]) -> None:
        self._folders = records
        self._folder_list.clear()
        for folder in records:
            display_name = str(folder.get("display_name", folder.get("raw_name", "")))
            item = QListWidgetItem(display_name, self._folder_list)
            item.setData(Qt.ItemDataRole.UserRole, folder.get("raw_name"))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if bool(folder.get("is_sync_target", 0))
                else Qt.CheckState.Unchecked
            )
        self._folder_status.setText("" if records else strings.SETTINGS_STATUS_NO_FOLDERS)

    def _operation_failed(self, error: object) -> None:
        self._operation = None
        self._close_progress()
        self._set_busy(False)
        self._folder_status.setText(
            present_error(
                error
                if isinstance(error, BaseException)
                else MailDockError("settings operation failed")
            ).message
        )

    def _operation_cancelled(self) -> None:
        self._operation = None
        self._close_progress()
        self._set_busy(False)

    def _set_busy(self, busy: bool) -> None:
        self._account_list.setEnabled(not busy)
        self._folder_list.setEnabled(not busy)
        self._add_account_button.setEnabled(not busy)
        self._edit_account_button.setEnabled(not busy and self._selected_account_id is not None)
        self._refresh_folders_button.setEnabled(not busy)
        self._save_folders_button.setEnabled(not busy)
        self._apply_button.setEnabled(not busy)
        if self._ok_button is not None:
            self._ok_button.setEnabled(not busy)

    def _current_settings(self) -> config.AppConfig:
        startup_verification = self._startup_verification.currentData()
        if startup_verification not in config.STARTUP_VERIFICATION_MODES:
            startup_verification = self._settings.startup_verification
        startup_verification = cast(str, startup_verification)
        storage_profiles = dict(self._settings.storage_profiles)
        root_uuid = getattr(self._context, "root_uuid", None) or self._settings.storage_root_uuid
        if isinstance(root_uuid, str) and root_uuid:
            profile = self._active_storage_profile()
            profile.update(
                {
                    "encryption": self._encryption_declaration(),
                    "encryption_declared_at": datetime.now(UTC).isoformat(),
                }
            )
            storage_profiles[root_uuid] = profile
        return replace(
            self._settings,
            max_message_bytes=self._max_message_size.value() * _MEGABYTE,
            block_remote_images=self._block_remote_images.isChecked(),
            sync_on_startup=self._sync_on_startup.isChecked(),
            startup_verification=startup_verification,
            storage_profiles=storage_profiles,
            credential_storage=self._credential_storage_mode(),
        )

    def _save_settings(self) -> bool:
        try:
            settings = self._current_settings()
            self._context.save_settings(settings)
        except Exception as error:
            self._folder_status.setText(present_error(error).message)
            return False
        self._settings = settings
        self.settings_saved.emit(settings)
        self._folder_status.setText(strings.SETTINGS_STATUS_SAVED)
        return True

    def _save_and_accept(self) -> None:
        if self._save_settings():
            self._stop_worker()
            super().accept()

    def _open_log_folder(self) -> None:
        path = config.config_dir() / "logs"
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def reject(self) -> None:
        self._close_progress()
        self._stop_worker()
        super().reject()

    def _stop_worker(self) -> None:
        self._worker.stop()

    def _close_progress(self) -> None:
        if self._progress_dialog is not None:
            self._progress_dialog.finish()
            self._progress_dialog.deleteLater()
            self._progress_dialog = None


def _test_connection(context: Any, values: dict[str, Any]) -> None:
    fetcher = context.create_fetcher_for_credentials(
        host=values["host"],
        port=values["port"],
        username=values["username"],
        password=values["password"],
    )
    with fetcher:
        return None
