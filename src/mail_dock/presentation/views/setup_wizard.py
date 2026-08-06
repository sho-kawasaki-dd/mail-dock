"""First-run setup wizard for storage, accounts, and sync targets."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWizard,
    QWizardPage,
)

from mail_dock.domain.accounts import validate_account_id
from mail_dock.domain.errors import MailDockError, StorageForeignRootError
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.repository import MessageRecord
from mail_dock.presentation import strings
from mail_dock.presentation.errors import present_error
from mail_dock.presentation.threads.worker import Worker
from mail_dock.usecases.register_account import register_account
from mail_dock.usecases.sync_folders import refresh_folders, set_sync_target

from .dialogs.progress_dialog import ProgressDialog

RootContextFactory = Callable[[Path], Any]
RootCapabilityProbe = Callable[[Path, str], Mapping[str, object]]
RootIdentityProbe = Callable[[Path], str]
RootInitializer = Callable[[Path], str]
RootSpaceChecker = Callable[[Path], str]
RootDriveKindResolver = Callable[[Path], str]
RootFreeSpaceResolver = Callable[[Path], int]


def _text(widget: QLineEdit) -> str:
    return widget.text().strip()


class SetupWizard(QWizard):
    """Collect and persist the three pieces of first-run setup state.

    ``on_root_confirmed`` is called while the root page is validated. The
    application uses it to start ``StorageSession`` only after the root has
    been initialized; the returned context is then used by the remaining
    pages. Operations that can touch IMAP or SQLite run through ``Worker``.
    """

    def __init__(
        self,
        initial_root: Path | None = None,
        *,
        context: Any | None = None,
        expected_root_uuid: str | None = None,
        on_root_confirmed: RootContextFactory | None = None,
        on_root_probe: RootCapabilityProbe | None = None,
        root_initializer: RootInitializer | None = None,
        on_root_identity_probe: RootIdentityProbe | None = None,
        check_root_space: RootSpaceChecker | None = None,
        resolve_drive_kind: RootDriveKindResolver | None = None,
        resolve_free_space: RootFreeSpaceResolver | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle(strings.WIZARD_TITLE)
        self._selected_root: Path | None = None
        self._context = context
        self._expected_root_uuid = expected_root_uuid
        self._on_root_confirmed = on_root_confirmed
        self._on_root_probe = on_root_probe
        self._on_root_identity_probe = on_root_identity_probe
        self._root_initializer = root_initializer
        self._check_root_space = check_root_space
        self._resolve_drive_kind = resolve_drive_kind
        self._resolve_free_space = resolve_free_space
        self._root_confirmed = context is not None
        self._storage_test_passed = False
        self._storage_check_root: Path | None = None
        self._storage_space_status: str | None = None
        self._storage_capability_level: str | None = None
        self._worker: Worker | None = None
        self._progress_dialog: ProgressDialog | None = None
        self._operation: str | None = None
        self._connection_test_passed = False
        self._account_id: str | None = None
        self._folder_records: tuple[MessageRecord, ...] = ()
        self._folder_checks: list[QCheckBox] = []

        self._build_root_page(initial_root)
        self._build_account_page()
        self._build_folders_page()

    @property
    def selected_root(self) -> Path | None:
        """Return the initialized root after root-page validation."""

        return self._selected_root

    @property
    def account_id(self) -> str | None:
        """Return the account registered by this wizard."""

        return self._account_id

    def _build_root_page(self, initial_root: Path | None) -> None:
        self._root_edit = QLineEdit(str(initial_root) if initial_root else "")
        self._root_page = QWizardPage()
        self._root_page.setTitle(strings.WIZARD_PAGE_STORAGE_TITLE)
        layout = QFormLayout(self._root_page)
        layout.addRow(strings.WIZARD_LABEL_STORAGE_ROOT, self._root_edit)
        browse_button = QPushButton(strings.WIZARD_BUTTON_BROWSE, self._root_page)
        browse_button.clicked.connect(self._browse)
        layout.addRow(browse_button)

        self._drive_kind_label = QLabel("-")
        self._free_space_label = QLabel("-")
        self._encryption_combo = QComboBox()
        self._encryption_combo.addItem(
            strings.WIZARD_ENCRYPTION_ENCRYPTED,
            "encrypted",
        )
        self._encryption_combo.addItem(
            strings.WIZARD_ENCRYPTION_UNENCRYPTED,
            "unencrypted",
        )
        self._encryption_combo.addItem(
            strings.WIZARD_ENCRYPTION_UNKNOWN,
            "unknown",
        )
        self._encryption_combo.setCurrentIndex(2)
        self._encryption_combo.currentIndexChanged.connect(self._update_encryption_confirmation)
        encryption_help = QLabel(strings.WIZARD_ENCRYPTION_DECLARATION_HELP)
        encryption_help.setWordWrap(True)
        self._encryption_confirmation = QCheckBox(
            strings.WIZARD_ENCRYPTION_UNENCRYPTED_CONFIRM_REQUIRED
        )
        self._encryption_confirmation.toggled.connect(self._update_root_page_state)
        self._update_encryption_confirmation()
        self._capability_label = QLabel("-")
        self._capability_label.setWordWrap(True)
        self._root_status = QLabel()
        self._root_status.setWordWrap(True)
        layout.addRow(strings.WIZARD_LABEL_DRIVE_KIND, self._drive_kind_label)
        layout.addRow(strings.WIZARD_LABEL_FREE_SPACE, self._free_space_label)
        layout.addRow(strings.WIZARD_LABEL_ENCRYPTION, self._encryption_combo)
        layout.addRow(encryption_help)
        layout.addRow(self._encryption_confirmation)
        layout.addRow(strings.WIZARD_LABEL_STORAGE_CAPABILITY, self._capability_label)
        layout.addRow(self._root_status)
        self._root_page_id = self.addPage(self._root_page)
        self._root_edit.textChanged.connect(self._update_root_preview)
        self._encryption_combo.currentIndexChanged.connect(self._invalidate_storage_test)
        self._update_root_preview()

        storage_test_button = QPushButton(strings.WIZARD_BUTTON_STORAGE_TEST, self._root_page)
        storage_test_button.clicked.connect(self._run_storage_checks)
        self._storage_test_button = storage_test_button
        layout.addRow(storage_test_button)

    def _update_root_preview(self, *_args: object) -> None:
        self._invalidate_storage_test()
        self._drive_kind_label.setText("-")
        self._free_space_label.setText("-")
        value = self._root_edit.text().strip()
        if not value or self._resolve_drive_kind is None or self._resolve_free_space is None:
            return
        root = Path(value).expanduser()
        try:
            drive_kind = self._resolve_drive_kind(root)
            free_bytes = self._resolve_free_space(root)
        except Exception:
            return
        self._drive_kind_label.setText(_drive_kind_text(drive_kind))
        self._free_space_label.setText(_format_bytes(free_bytes))

    def _invalidate_storage_test(self, *_args: object) -> None:
        self._storage_test_passed = False
        self._storage_check_root = None
        self._storage_space_status = None
        self._storage_capability_level = None
        self._selected_root = None
        self._capability_label.setText("-")
        self._root_status.clear()

    def _encryption_declaration(self) -> str:
        value = self._encryption_combo.currentData()
        return value if isinstance(value, str) else "unknown"

    def _update_encryption_confirmation(self, *_args: object) -> None:
        required = self._encryption_declaration() == "unencrypted"
        self._encryption_confirmation.setVisible(required)
        self._encryption_confirmation.setEnabled(required)
        if not required:
            self._encryption_confirmation.setChecked(False)

    def _update_root_page_state(self, *_args: object) -> None:
        self._root_page.completeChanged.emit()

    def _build_account_page(self) -> None:
        self._account_page = QWizardPage()
        self._account_page.setTitle(strings.WIZARD_PAGE_ACCOUNT_TITLE)
        layout = QFormLayout(self._account_page)
        self._account_id_edit = QLineEdit()
        self._host_edit = QLineEdit()
        self._port_edit = QSpinBox()
        self._port_edit.setRange(1, 65535)
        self._port_edit.setValue(993)
        self._username_edit = QLineEdit()
        self._password_edit = QLineEdit()
        self._password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._display_name_edit = QLineEdit()
        for field in (
            self._account_id_edit,
            self._host_edit,
            self._username_edit,
            self._password_edit,
        ):
            field.textChanged.connect(self._invalidate_connection_test)
        self._port_edit.valueChanged.connect(self._invalidate_connection_test)
        layout.addRow(strings.WIZARD_LABEL_ACCOUNT_ID, self._account_id_edit)
        layout.addRow(strings.WIZARD_LABEL_HOST, self._host_edit)
        layout.addRow(strings.WIZARD_LABEL_PORT, self._port_edit)
        layout.addRow(strings.WIZARD_LABEL_USERNAME, self._username_edit)
        layout.addRow(strings.WIZARD_LABEL_PASSWORD, self._password_edit)
        layout.addRow(strings.WIZARD_LABEL_DISPLAY_NAME, self._display_name_edit)
        self._connection_test_button = QPushButton(
            strings.WIZARD_BUTTON_CONNECTION_TEST,
            self._account_page,
        )
        self._connection_test_button.clicked.connect(self._test_connection)
        layout.addRow(self._connection_test_button)
        self._account_status = QLabel()
        self._account_status.setWordWrap(True)
        layout.addRow(self._account_status)
        self._account_page_id = self.addPage(self._account_page)

    def _build_folders_page(self) -> None:
        self._folders_page = QWizardPage()
        self._folders_page.setTitle(strings.WIZARD_PAGE_FOLDERS_TITLE)
        layout = QVBoxLayout(self._folders_page)
        help_label = QLabel(strings.WIZARD_HELP_FOLDER_SYNC_TARGET)
        help_label.setWordWrap(True)
        layout.addWidget(help_label)
        self._folders_status = QLabel()
        self._folders_status.setWordWrap(True)
        layout.addWidget(self._folders_status)
        self._folders_layout = QVBoxLayout()
        layout.addLayout(self._folders_layout)
        self._folders_page_id = self.addPage(self._folders_page)
        self._set_finish_enabled(False)

    def validateCurrentPage(self) -> bool:  # noqa: N802
        """Validate the active page and perform its setup action."""

        if self.currentId() == self._root_page_id:
            return self._validate_root()
        if self.currentId() == self._account_page_id:
            return self._validate_account()
        if self.currentId() == self._folders_page_id:
            return self._validate_folders()
        return super().validateCurrentPage()

    def initializePage(self, page_id: int) -> None:  # noqa: N802
        """Start folder discovery when the folder page becomes visible."""

        super().initializePage(page_id)
        if page_id == self._folders_page_id and self._context is not None:
            self._load_folders()

    def _run_storage_checks(self, *_args: object) -> bool:
        value = self._root_edit.text().strip()
        if not value:
            self._root_status.setText(strings.ERROR_STORAGE_ROOT_MISSING)
            self._root_edit.setFocus()
            return False
        root = Path(value).expanduser()
        try:
            if (
                self._root_initializer is None
                or self._check_root_space is None
                or self._resolve_drive_kind is None
                or self._resolve_free_space is None
            ):
                raise MailDockError("Storage root operations are not configured")
            existing_root_probe = "missing"
            if self._on_root_identity_probe is not None:
                existing_root_probe = self._on_root_identity_probe(root)
                if existing_root_probe not in {"missing", "ok", "foreign"}:
                    raise MailDockError("Storage root identity result is invalid")
                if existing_root_probe == "foreign":
                    raise StorageForeignRootError(strings.ERROR_FOREIGN_ROOT)
            root_uuid = self._root_initializer(root)
            if (
                existing_root_probe != "missing"
                and self._expected_root_uuid is not None
                and root_uuid != self._expected_root_uuid
            ):
                raise StorageForeignRootError(strings.ERROR_FOREIGN_ROOT)
            drive_kind = self._resolve_drive_kind(root)
            free_bytes = self._resolve_free_space(root)
            self._drive_kind_label.setText(_drive_kind_text(drive_kind))
            self._free_space_label.setText(_format_bytes(free_bytes))
            status = self._check_root_space(root)
        except Exception as error:
            self._show_inline_error(self._root_status, error)
            self._capability_label.setText("-")
            return False

        self._selected_root = root.resolve(strict=False)
        encryption = self._encryption_declaration()
        if encryption == "unencrypted" and not self._encryption_confirmation.isChecked():
            self._root_status.setText(strings.WIZARD_ENCRYPTION_UNENCRYPTED_CONFIRM_REQUIRED)
            return False
        capability_level: str | None = None
        if self._on_root_probe is not None:
            try:
                self._capability_label.setText(strings.WIZARD_STATUS_TESTING_STORAGE)
                result = self._on_root_probe(self._selected_root, encryption)
                raw_capability_level = result.get("capability_level")
                if not isinstance(raw_capability_level, str):
                    raise MailDockError("Storage capability result is invalid")
                capability_level = raw_capability_level
            except Exception as error:
                self._show_inline_error(self._root_status, error)
                self._capability_label.setText("-")
                return False
            self._capability_label.setText(_capability_text(capability_level))
            if capability_level == "unsupported":
                self._storage_test_passed = False
                self._root_status.setText(strings.WIZARD_CAPABILITY_UNSUPPORTED_DESCRIPTION)
                return False

        if capability_level == "degraded":
            status_text = strings.WIZARD_CAPABILITY_DEGRADED_DESCRIPTION
        else:
            status_text = (
                strings.WIZARD_WARNING_SPACE
                if status == "warning"
                else strings.WIZARD_STATUS_ROOT_READY
            )
        self._root_status.setText(status_text)
        self._storage_test_passed = True
        self._storage_check_root = self._selected_root
        self._storage_space_status = status
        self._storage_capability_level = capability_level
        return True

    def _validate_root(self) -> bool:
        if self._on_root_probe is not None and not self._storage_test_passed:
            self._root_status.setText(strings.WIZARD_STATUS_STORAGE_TEST_REQUIRED)
            return False
        if not self._storage_test_passed and not self._run_storage_checks():
            return False
        if self._selected_root is None:
            self._root_status.setText(strings.ERROR_STORAGE_ROOT_MISSING)
            return False
        if not self._root_confirmed and self._on_root_confirmed is not None:
            try:
                self._context = self._on_root_confirmed(self._selected_root)
            except Exception as error:
                self._show_inline_error(self._root_status, error)
                return False
            self._root_confirmed = True
        return self._context is not None

    def _validate_account(self) -> bool:
        account_id = _text(self._account_id_edit)
        host = _text(self._host_edit)
        username = _text(self._username_edit)
        password = self._password_edit.text()
        if not account_id or not host or not username or not password:
            self._account_status.setText(strings.WIZARD_STATUS_ACCOUNT_REQUIRED)
            return False
        try:
            validate_account_id(account_id)
        except Exception as error:
            self._show_inline_error(self._account_status, error)
            return False
        if not self._connection_test_passed:
            self._account_status.setText(strings.WIZARD_STATUS_CONNECTION_REQUIRED)
            return False
        if self._context is None:
            self._account_status.setText(strings.ERROR_STARTUP_FAILED)
            return False
        try:
            register_account(
                self._context.create_message_repository(),
                self._context.credential_store,
                account_id=account_id,
                host=host,
                port=self._port_edit.value(),
                username=username,
                password=password,
                display_name=_text(self._display_name_edit) or None,
            )
        except Exception as error:
            self._show_inline_error(self._account_status, error)
            return False
        self._account_id = account_id
        return True

    def _validate_folders(self) -> bool:
        if not self._folder_checks or not any(check.isChecked() for check in self._folder_checks):
            self._folders_status.setText(strings.WIZARD_STATUS_FOLDER_SELECTION_REQUIRED)
            return False
        if self._context is None or self._account_id is None:
            return False
        try:
            repository = self._context.create_message_repository()
            for folder, check in zip(self._folder_records, self._folder_checks, strict=True):
                raw_name = folder.get("raw_name")
                if isinstance(raw_name, str):
                    set_sync_target(repository, self._account_id, raw_name, check.isChecked())
        except Exception as error:
            self._show_inline_error(self._folders_status, error)
            return False
        return True

    def _test_connection(self) -> None:
        if self._context is None:
            return
        account_id = _text(self._account_id_edit)
        host = _text(self._host_edit)
        username = _text(self._username_edit)
        password = self._password_edit.text()
        if not account_id or not host or not username or not password:
            self._account_status.setText(strings.WIZARD_STATUS_ACCOUNT_REQUIRED)
            return
        try:
            validate_account_id(account_id)
        except Exception as error:
            self._show_inline_error(self._account_status, error)
            return

        self._connection_test_passed = False
        self._connection_test_button.setEnabled(False)
        self._account_status.setText(strings.WIZARD_STATUS_TESTING_CONNECTION)
        token = self._submit_operation(
            "connection",
            lambda: _test_connection(
                self._context,
                host=host,
                port=self._port_edit.value(),
                username=username,
                password=password,
            ),
        )
        self._show_progress(strings.WIZARD_STATUS_TESTING_CONNECTION, token)

    def _invalidate_connection_test(self, *_args: object) -> None:
        self._connection_test_passed = False

    def _load_folders(self) -> None:
        account_id = self._account_id
        if self._operation == "folders" or account_id is None:
            return
        self._folders_status.setText(strings.WIZARD_STATUS_FOLDER_LOADING)
        token = self._submit_operation(
            "folders",
            lambda: self._refresh_account_folders(account_id),
        )
        self._show_progress(strings.WIZARD_STATUS_FOLDER_LOADING, token)

    def _refresh_account_folders(self, account_id: str) -> tuple[MessageRecord, ...]:
        if self._context is None:
            return ()
        repository = self._context.create_message_repository()
        account = next(
            (item for item in repository.list_accounts() if item.get("id") == account_id),
            None,
        )
        if account is None:
            raise MailDockError(strings.WIZARD_STATUS_NO_ACCOUNT)
        fetcher = self._context.create_fetcher(account)
        with fetcher:
            refresh_folders(fetcher, repository, account_id)
        return tuple(repository.list_folders(account_id))

    def _submit_operation(self, operation: str, callback: Callable[[], object]) -> CancelToken:
        self._stop_worker()
        connection_manager = getattr(self._context, "connection_manager", None)
        worker = Worker(connection_manager)
        self._worker = worker
        self._operation = operation
        worker.result.connect(self._operation_succeeded)
        worker.failed.connect(self._operation_failed)
        worker.cancelled.connect(self._operation_cancelled)
        worker.start()
        return worker.submit(callback)

    def _show_progress(self, message: str, token: CancelToken) -> None:
        self._close_progress()
        dialog = ProgressDialog(message, self)
        dialog.attach_token(token)
        self._progress_dialog = dialog
        dialog.show()

    def _operation_succeeded(self, value: object) -> None:
        operation = self._operation
        self._operation = None
        self._close_progress()
        if operation == "connection":
            self._connection_test_passed = True
            self._connection_test_button.setEnabled(True)
            self._account_status.setText(strings.WIZARD_STATUS_CONNECTION_OK)
        elif operation == "folders":
            records = value if isinstance(value, tuple) else ()
            self._set_folder_checks(records)
        self._stop_worker()

    def _operation_failed(self, error: object) -> None:
        operation = self._operation
        self._operation = None
        self._close_progress()
        safe_error = error if isinstance(error, BaseException) else MailDockError("setup failed")
        message = present_error(safe_error).message
        if operation == "connection":
            self._connection_test_button.setEnabled(True)
            self._account_status.setText(message)
        elif operation == "folders":
            self._folders_status.setText(message)
            self._set_finish_enabled(False)
        self._stop_worker()

    def _operation_cancelled(self) -> None:
        self._operation = None
        self._close_progress()
        self._stop_worker()

    def _set_folder_checks(self, records: tuple[MessageRecord, ...]) -> None:
        self._folder_records = records
        self._folder_checks.clear()
        while self._folders_layout.count():
            item = self._folders_layout.takeAt(0)
            if item is None:
                break
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for record in records:
            display_name = record.get("display_name", record.get("raw_name", ""))
            check = QCheckBox(str(display_name), self._folders_page)
            check.setChecked(bool(record.get("is_sync_target", 0)))
            self._folder_checks.append(check)
            self._folders_layout.addWidget(check)
        self._folders_status.setText("" if records else strings.WIZARD_STATUS_FOLDER_EMPTY)
        self._set_finish_enabled(bool(records))

    def _set_finish_enabled(self, enabled: bool) -> None:
        button = self.button(QWizard.WizardButton.FinishButton)
        if button is not None:
            button.setEnabled(enabled)

    def _show_inline_error(self, label: QLabel, error: BaseException) -> None:
        label.setText(present_error(error).message)

    def reject(self) -> None:
        self._close_progress()
        self._stop_worker()
        super().reject()

    def _stop_worker(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.stop()

    def _close_progress(self) -> None:
        if self._progress_dialog is not None:
            self._progress_dialog.finish()
            self._progress_dialog.deleteLater()
            self._progress_dialog = None

    def _browse(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, strings.WIZARD_DIALOG_SELECT_ROOT)
        if selected:
            self._root_edit.setText(selected)


def _test_connection(
    context: Any,
    *,
    host: str,
    port: int,
    username: str,
    password: str,
) -> None:
    fetcher = context.create_fetcher_for_credentials(
        host=host,
        port=port,
        username=username,
        password=password,
    )
    with fetcher:
        return None


def _drive_kind_text(kind: str | StrEnum) -> str:
    return kind.value if isinstance(kind, StrEnum) else kind


def _capability_text(level: str) -> str:
    labels = {
        "ok": strings.WIZARD_CAPABILITY_OK,
        "degraded": strings.WIZARD_CAPABILITY_DEGRADED,
        "unsupported": strings.WIZARD_CAPABILITY_UNSUPPORTED,
    }
    return labels.get(level, level)


def _format_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return "unknown"
