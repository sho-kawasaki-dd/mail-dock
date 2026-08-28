"""Main application window and presentation composition for the GUI shell."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, cast

from PySide6.QtCore import (
    QItemSelectionModel,
    QModelIndex,
    QPoint,
    QSettings,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStatusBar,
    QStyle,
    QSystemTrayIcon,
    QToolBar,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from mail_dock import config
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.messages import AttachmentSavePlan, SavedFile
from mail_dock.domain.ports import BaseIntegrityStorage
from mail_dock.domain.search import MessageDetail
from mail_dock.domain.storage_state import StorageState
from mail_dock.presentation import strings
from mail_dock.presentation.context import AppContext
from mail_dock.presentation.errors import user_message
from mail_dock.presentation.models.folder_tree_model import (
    FolderTreeModel,
    build_mail_account_roots,
)
from mail_dock.presentation.models.message_table_model import MessageTableModel
from mail_dock.presentation.threads.query_worker import QueryWorker
from mail_dock.presentation.threads.sync_worker import (
    FolderTreeSnapshot,
    SyncErrorNotification,
    SyncWorker,
)
from mail_dock.presentation.threads.verify_worker import VerifyWorker
from mail_dock.presentation.viewmodels.message_list_viewmodel import MessageListViewModel
from mail_dock.presentation.views.detail_view import AttachmentSaveRequest, DetailView
from mail_dock.presentation.views.dialogs.audit_log_dialog import AuditLogDialog
from mail_dock.presentation.views.dialogs.confirmation_dialog import (
    ConfirmationDialog,
    confirm_overwrite,
    confirm_save_executable,
)
from mail_dock.presentation.views.dialogs.delete_remote_dialog import (
    DeleteConfirmationDialog,
    DeleteDryRunDialog,
)
from mail_dock.presentation.views.dialogs.failure_review_dialog import FailureReviewDialog
from mail_dock.presentation.views.dialogs.integrity_dialog import IntegrityDialog
from mail_dock.presentation.views.dialogs.settings_dialog import SettingsDialog
from mail_dock.presentation.views.message_list import MessageListSearchBar, MessageListView
from mail_dock.usecases.delete_remote import DeleteDryRunResult, DeleteResult
from mail_dock.usecases.export_attachments import ExportAttachmentsProgress, ExportAttachmentsResult
from mail_dock.usecases.export_mbox import ExportMboxProgress
from mail_dock.usecases.reindex import ReindexResult
from mail_dock.usecases.sync_folders import FolderRefreshResult
from mail_dock.usecases.sync_mail import SyncOptions, SyncProgress, SyncResult


class _StorageWriteGate:
    """Expose the monitor's current state to the purge use case."""

    state = StorageState.ATTACHED

    def is_write_allowed(self) -> bool:
        return self.state is StorageState.ATTACHED

    def is_remote_delete_allowed(self) -> bool:
        return self.is_write_allowed()


class MainWindow(QMainWindow):
    """Own the visible application window and its two presentation workers."""

    sync_requested = Signal()
    refresh_folders_requested = Signal()
    settings_requested = Signal()
    export_requested = Signal()

    def __init__(
        self,
        context: AppContext,
        *,
        on_storage_root_switch: Callable[[Path], None] | None = None,
        on_storage_setup: Callable[[Path | None], None] | None = None,
        on_storage_detach: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self.context = context
        self._on_storage_root_switch = on_storage_root_switch
        self._on_storage_setup = on_storage_setup
        self._on_storage_detach = on_storage_detach
        self.setObjectName("mainWindow")
        self.setWindowTitle(strings.MAIN_WINDOW_TITLE)
        self._workers_stopped = False
        self._sync_token: CancelToken | None = None
        self._folder_refresh_token: CancelToken | None = None
        self._file_token: CancelToken | None = None
        self._export_list_token: CancelToken | None = None
        self._pending_export_kind: str | None = None
        self._pending_export_destination: Path | None = None
        self._active_export_kind: str | None = None
        self._active_export_count = 0
        self._pending_attachment_request: AttachmentSaveRequest | None = None
        self._pending_attachment_plan: AttachmentSavePlan | None = None
        self._query_busy = False
        self._verify_dialog: IntegrityDialog | None = None
        self._failure_review_dialog: FailureReviewDialog | None = None
        self._audit_log_dialog: AuditLogDialog | None = None
        self._operation_gate = Lock()
        self._storage_write_gate = _StorageWriteGate()
        self._ui_settings = QSettings("mail-dock", "mail-dock")
        self._exit_requested = False
        self._close_handled = False
        self._tray_icon: QSystemTrayIcon | None = None
        self._tray_sync_action: QAction | None = None
        self.sync_timer = QTimer(self)
        self.sync_timer.setInterval(context.settings.sync_interval_minutes * 60 * 1000)
        self.sync_timer.timeout.connect(self._start_scheduled_sync)

        self.query_worker = QueryWorker(
            context.create_search_repository,
            storage_factory=context.create_eml_storage,
            renderer_factory=context.create_message_renderer,
            connection_manager=context.connection_manager,
        )
        self.sync_worker = SyncWorker(
            context.create_message_repository,
            context.create_fetcher,
            context.create_eml_storage,
            context.create_manifest_writer,
            renderer_factory=context.create_message_renderer,
            sync_options=SyncOptions(
                max_message_bytes=context.settings.max_message_bytes,
                flag_refresh_enabled=context.settings.flag_refresh_enabled,
                flag_refresh_window_days=context.settings.flag_refresh_window_days,
                flag_refresh_min_interval_seconds=context.settings.flag_refresh_min_interval_seconds,
            ),
            connection_manager=context.connection_manager,
            operation_gate=self._operation_gate,
        )
        self.query_worker.start()
        self.sync_worker.start()
        self.verify_worker = self._build_verify_worker()
        self.verify_worker.start()

        query_worker = cast(Any, self.query_worker)
        self.message_list_viewmodel = MessageListViewModel(query_worker, self)
        self.message_table_model = MessageTableModel(
            query_worker,
            self,
            page_size=MessageListViewModel.DEFAULT_PAGE_SIZE,
            trash_grace_days=context.settings.trash_grace_days,
        )
        self.message_list_view = MessageListView(
            self.message_table_model,
            viewmodel=self.message_list_viewmodel,
            worker=query_worker,
        )
        self.search_bar = MessageListSearchBar(self.message_list_viewmodel, self)
        self.detail_view = DetailView(
            query_worker,
            context.create_html_sanitizer(),
            self,
            block_remote_images=context.settings.block_remote_images,
        )
        self.folder_tree_model = FolderTreeModel(
            cast(Sequence[Any], getattr(context, "folder_tree_roots", ())),
            self,
        )
        self.folder_tree_view = QTreeView(self)
        self.folder_tree_view.setObjectName("folderTreeView")
        self.folder_tree_view.setHeaderHidden(True)
        self.folder_tree_view.setModel(self.folder_tree_model)

        self._build_central_layout()
        self._build_actions()
        self._build_status_bar()
        self.set_storage_encryption(getattr(context, "encryption_declaration", "unknown"))
        self.set_storage_capability(getattr(context, "capability_level", None))
        self._set_credential_storage_status(getattr(context, "credential_storage", None))
        self._connect_presentation()
        self._setup_tray()
        if context.settings.sync_interval_minutes > 0:
            self.sync_timer.start()
        self._restore_ui_state()

    def start_startup_sync(self) -> None:
        """Start one synchronization run for all enabled accounts."""

        if self._sync_token is not None:
            return
        if not self._prepare_sync(self._enabled_account_ids()):
            return
        sync_all_accounts = getattr(self.sync_worker, "sync_all_accounts", None)
        if callable(sync_all_accounts):
            token = sync_all_accounts()
            if isinstance(token, CancelToken):
                self._start_sync(token)
        else:
            self._sync_selected_account()

    def stop_workers(self) -> None:
        """Stop all GUI workers before the storage session closes."""

        if self._workers_stopped:
            return
        self._workers_stopped = True
        self.message_table_model.stop_loading()
        self.message_list_viewmodel.cancel_search()
        self.sync_worker.stop()
        self.query_worker.stop()
        self.verify_worker.stop()
        self.context.stop_workers()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        """Hide to the tray, or persist the UI shell before an explicit exit."""

        if not self._exit_requested and not self._workers_stopped and self._tray_icon is not None:
            self.hide()
            event.ignore()
            return
        if self._close_handled:
            event.accept()
            return

        self._close_handled = True
        self._save_ui_state()
        self.sync_timer.stop()
        if self._tray_icon is not None:
            self._tray_icon.hide()
        self.detail_view.close()
        self.stop_workers()
        super().closeEvent(event)

    def _build_central_layout(self) -> None:
        middle = QWidget(self)
        middle.setObjectName("messageListPane")
        middle_layout = QVBoxLayout(middle)
        middle_layout.setContentsMargins(0, 0, 0, 0)
        middle_layout.addWidget(self.message_list_view, 1)

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setObjectName("mainSplitter")
        self.splitter.addWidget(self.folder_tree_view)
        self.splitter.addWidget(middle)
        self.splitter.addWidget(self.detail_view)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 1)

        central = QWidget(self)
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        self._storage_detached_banner = QLabel(strings.BANNER_STORAGE_DETACHED, central)
        self._storage_detached_banner.setObjectName("storageDetachedBanner")
        self._storage_detached_banner.setWordWrap(True)
        self._storage_detached_banner.setVisible(False)
        central_layout.addWidget(self._storage_detached_banner)
        central_layout.addWidget(self.splitter, 1)
        self.setCentralWidget(central)

    def _build_actions(self) -> None:
        self.sync_action = QAction(strings.MAIN_TOOLBAR_SYNC, self)
        self.refresh_folders_action = QAction(strings.MAIN_TOOLBAR_REFRESH_FOLDERS, self)
        self.settings_action = QAction(strings.MAIN_TOOLBAR_SETTINGS, self)
        self.export_eml_action = QAction(strings.MAIN_MENU_EXPORT_EML, self)
        self.export_mbox_action = QAction(strings.MAIN_MENU_EXPORT_MBOX, self)
        self.export_attachments_action = QAction(strings.MAIN_MENU_EXPORT_ATTACHMENTS, self)
        self.delete_remote_action = QAction(strings.MAIN_MENU_DELETE_REMOTE, self)
        self.delete_remote_action.setEnabled(False)
        self.restore_trash_action = QAction(strings.MAIN_MENU_RESTORE_TRASH, self)
        self.purge_trash_action = QAction(strings.MAIN_MENU_PURGE_TRASH, self)
        self.restore_trash_action.setEnabled(False)
        self.purge_trash_action.setEnabled(False)
        self.thread_view_action = QAction(strings.MAIN_MENU_THREAD_VIEW, self)
        self.integrity_action = QAction(strings.MAIN_MENU_INTEGRITY, self)
        self.failure_review_action = QAction(strings.FAILURE_REVIEW_TITLE, self)
        self.audit_log_action = QAction(strings.MAIN_MENU_AUDIT_LOG, self)
        self.exit_action = QAction(strings.MAIN_MENU_EXIT, self)
        self.open_log_folder_action = QAction(strings.MAIN_MENU_OPEN_LOG_FOLDER, self)
        self.storage_info_action = QAction(strings.MAIN_MENU_STORAGE_INFO, self)
        self.storage_switch_action = QAction(strings.MAIN_MENU_STORAGE_SWITCH, self)
        self.storage_setup_action = QAction(strings.MAIN_MENU_STORAGE_SETUP, self)
        self.storage_detach_action = QAction(strings.MAIN_MENU_STORAGE_DETACH, self)
        self.storage_detach_action.setEnabled(self._on_storage_detach is not None)

        toolbar = QToolBar(strings.APP_NAME, self)
        toolbar.setObjectName("mainToolBar")
        toolbar.addAction(self.sync_action)
        toolbar.addAction(self.refresh_folders_action)
        toolbar.addSeparator()
        toolbar.addWidget(self.search_bar)
        toolbar.addSeparator()
        toolbar.addAction(self.settings_action)
        toolbar.addSeparator()
        toolbar.addAction(self.delete_remote_action)
        toolbar.addAction(self.restore_trash_action)
        toolbar.addAction(self.purge_trash_action)
        self.addToolBar(toolbar)

        file_menu = self.menuBar().addMenu(strings.MAIN_MENU_FILE)
        file_menu.addAction(self.export_eml_action)
        file_menu.addAction(self.export_mbox_action)
        file_menu.addAction(self.export_attachments_action)
        file_menu.addAction(self.delete_remote_action)
        file_menu.addAction(self.restore_trash_action)
        file_menu.addAction(self.purge_trash_action)
        file_menu.addSeparator()
        file_menu.addAction(self.exit_action)
        view_menu = self.menuBar().addMenu(strings.MAIN_MENU_VIEW)
        view_menu.addAction(self.thread_view_action)
        tools_menu = self.menuBar().addMenu(strings.MAIN_MENU_TOOLS)
        tools_menu.addAction(self.integrity_action)
        tools_menu.addAction(self.failure_review_action)
        tools_menu.addAction(self.audit_log_action)
        storage_menu = self.menuBar().addMenu(strings.MAIN_MENU_STORAGE)
        storage_menu.addAction(self.storage_info_action)
        storage_menu.addAction(self.storage_switch_action)
        storage_menu.addAction(self.storage_setup_action)
        storage_menu.addSeparator()
        storage_menu.addAction(self.storage_detach_action)
        help_menu = self.menuBar().addMenu(strings.MAIN_MENU_HELP)
        help_menu.addAction(self.open_log_folder_action)
        self.encryption_help_action = QAction(strings.MAIN_MENU_HELP_ENCRYPTION, self)
        help_menu.addAction(self.encryption_help_action)

        self.sync_action.triggered.connect(self._sync_selected_account)
        self.refresh_folders_action.triggered.connect(self._refresh_selected_account)
        self.settings_action.triggered.connect(self.settings_requested)
        self.export_eml_action.triggered.connect(self._export_current_message)
        self.export_mbox_action.triggered.connect(self._export_mbox)
        self.export_attachments_action.triggered.connect(self._export_attachments)
        self.delete_remote_action.triggered.connect(self._start_remote_delete)
        self.restore_trash_action.triggered.connect(self._restore_selected_from_trash)
        self.purge_trash_action.triggered.connect(self._purge_selected_from_trash)
        self.thread_view_action.triggered.connect(self.detail_view.request_thread)
        self.exit_action.triggered.connect(self._request_exit)
        self.settings_requested.connect(self._show_settings)
        self.integrity_action.triggered.connect(self._show_integrity_dialog)
        self.failure_review_action.triggered.connect(self._show_failure_review)
        self.audit_log_action.triggered.connect(self._show_audit_log)
        self.open_log_folder_action.triggered.connect(self._open_log_folder)
        self.encryption_help_action.triggered.connect(self._open_encryption_guide)
        self.storage_info_action.triggered.connect(self._show_storage_root)
        self.storage_switch_action.triggered.connect(self._request_storage_switch)
        self.storage_setup_action.triggered.connect(self._request_storage_setup)
        self.storage_detach_action.triggered.connect(self._request_storage_detach)
        self.message_list_view.customContextMenuRequested.connect(
            self._show_message_list_context_menu
        )

    def _build_status_bar(self) -> None:
        status = QStatusBar(self)
        self.setStatusBar(status)
        self._status_label = QLabel(strings.STATUS_READY, self)
        self._count_label = QLabel(strings.STATUS_MESSAGE_COUNT.format(count=0), self)
        self._storage_status_label = QLabel(strings.STATUS_STORAGE_CONNECTED, self)
        self._storage_root_label = QLabel(self)
        self._storage_root_label.setObjectName("storageRootLabel")
        self._encryption_status_label = QLabel(self)
        self._encryption_status_label.setObjectName("storageEncryptionStatusLabel")
        self._credential_status_label = QLabel(self)
        self._credential_status_label.setObjectName("credentialStorageStatusLabel")
        self._credential_status_label.setVisible(False)
        self._progress_bar = QProgressBar(self)
        self._progress_bar.setObjectName("syncProgressBar")
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setFixedWidth(160)
        self._cancel_button = QPushButton(strings.SEARCH_CANCEL, self)
        self._cancel_button.setObjectName("operationCancelButton")
        self._cancel_button.setEnabled(False)
        status.addWidget(self._status_label, 1)
        status.addPermanentWidget(self._count_label)
        status.addPermanentWidget(self._progress_bar)
        status.addPermanentWidget(self._cancel_button)
        status.addPermanentWidget(self._storage_root_label)
        status.addPermanentWidget(self._storage_status_label)
        status.addPermanentWidget(self._encryption_status_label)
        status.addPermanentWidget(self._credential_status_label)
        self.set_storage_root(getattr(self.context, "storage_root", None))

    def set_storage_root(self, root: object) -> None:
        """Display the absolute path of the active storage root."""

        path = str(root) if isinstance(root, Path) else "-"
        self._storage_root_label.setText(strings.STATUS_STORAGE_ROOT.format(path=path))
        self._storage_root_label.setToolTip(path)

    def has_active_operations(self) -> bool:
        """Return whether an operation must be stopped before releasing storage."""

        return any(
            (
                self._sync_token is not None,
                self._folder_refresh_token is not None,
                self._file_token is not None,
                self._export_list_token is not None,
                self._query_busy,
                bool(self.verify_worker.active_tokens),
            )
        )

    def _show_storage_root(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle(strings.DIALOG_STORAGE_ROOT_INFO_TITLE)
        form = QFormLayout(dialog)
        form.addRow(strings.DIALOG_STORAGE_ROOT_PATH, QLabel(str(self.context.storage_root)))
        form.addRow(strings.DIALOG_STORAGE_ROOT_UUID, QLabel(str(self.context.root_uuid or "-")))
        form.addRow(
            strings.DIALOG_STORAGE_ROOT_CAPABILITY,
            QLabel(str(getattr(self.context, "capability_level", None) or "-")),
        )
        form.addRow(
            strings.DIALOG_STORAGE_ROOT_ENCRYPTION,
            QLabel(str(getattr(self.context, "encryption_declaration", "unknown"))),
        )
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok, parent=dialog)
        buttons.accepted.connect(dialog.accept)
        form.addRow(buttons)
        dialog.exec()

    def _request_storage_switch(self) -> None:
        if self._on_storage_root_switch is None:
            return
        selected = QFileDialog.getExistingDirectory(self, strings.MAIN_MENU_STORAGE_SWITCH)
        if not selected:
            return
        if (
            self.has_active_operations()
            and not ConfirmationDialog(
                strings.DIALOG_CONFIRM_STORAGE_SWITCH_BUSY,
                self,
            ).confirmed()
        ):
            return
        self._on_storage_root_switch(Path(selected))

    def _request_storage_setup(self) -> None:
        if self._on_storage_setup is None:
            return
        if (
            self.has_active_operations()
            and not ConfirmationDialog(
                strings.DIALOG_CONFIRM_STORAGE_SETUP_BUSY,
                self,
            ).confirmed()
        ):
            return
        self._on_storage_setup(None)

    def _request_storage_detach(self) -> None:
        if self._on_storage_detach is not None:
            self._on_storage_detach()

    def set_storage_encryption(self, state: object) -> None:
        """Display the user-declared storage encryption state."""

        declaration = state if isinstance(state, str) else "unknown"
        if declaration not in config.ENCRYPTION_DECLARATIONS:
            declaration = "unknown"
        text = strings.STATUS_STORAGE_ENCRYPTION.format(state=declaration)
        self._encryption_status_label.setText(text)
        self._encryption_status_label.setToolTip(f"{text}\n{strings.MAIN_MENU_HELP_ENCRYPTION}")

    def set_storage_capability(self, level: object) -> None:
        """Display the measured storage capability, including warnings."""

        capability = level.lower() if isinstance(level, str) else "unknown"
        if capability == "unsupported":
            text = strings.STATUS_STORAGE_CAPABILITY_UNSUPPORTED
        elif capability == "degraded":
            text = strings.STATUS_STORAGE_CAPABILITY_DEGRADED
        elif capability == "ok":
            text = strings.STATUS_STORAGE_CAPABILITY.format(level="OK")
        else:
            text = strings.STATUS_STORAGE_CAPABILITY.format(level=capability)
        self._storage_status_label.setText(text)
        self._storage_status_label.setToolTip(text)

    def _set_credential_storage_status(self, mode: object) -> None:
        if mode == "session_only":
            self._credential_status_label.setText(strings.STATUS_CREDENTIAL_STORAGE_SESSION_ONLY)
            self._credential_status_label.setVisible(True)
        else:
            self._credential_status_label.clear()
            self._credential_status_label.setVisible(False)

    def _prepare_sync(self, account_ids: Sequence[str]) -> bool:
        """Run confirmation and credential gates before queueing a sync."""

        if not self._confirm_first_sync_encryption():
            return False
        return self._ensure_session_credentials(account_ids)

    def _confirm_first_sync_encryption(self) -> bool:
        root_uuid = getattr(self.context, "root_uuid", None)
        settings = getattr(self.context, "settings", None)
        if not isinstance(root_uuid, str) or not isinstance(settings, config.AppConfig):
            return True
        raw_profile = settings.storage_profiles.get(root_uuid)
        profile = dict(raw_profile) if isinstance(raw_profile, dict) else {}
        encryption = getattr(
            self.context,
            "encryption_declaration",
            profile.get("encryption", "unknown"),
        )
        if encryption not in {"unencrypted", "unknown"}:
            return True
        if profile.get("first_sync_confirmed_at"):
            return True
        if not ConfirmationDialog(
            strings.DIALOG_CONFIRM_FIRST_SYNC_WITHOUT_ENCRYPTION,
            self,
        ).confirmed():
            return False
        profile["first_sync_confirmed_at"] = datetime.now(UTC).isoformat()
        profiles = dict(settings.storage_profiles)
        profiles[root_uuid] = profile
        try:
            self.context.save_settings(replace(settings, storage_profiles=profiles))
        except Exception:
            self._status_label.setText(strings.ERROR_CONFIG)
            return False
        return True

    def _ensure_session_credentials(self, account_ids: Sequence[str]) -> bool:
        if getattr(self.context, "credential_storage", None) != "session_only":
            return True
        store = getattr(self.context, "credential_store", None)
        if store is None:
            return True
        for account_id in account_ids:
            if store.get_password(account_id) is not None:
                continue
            password, accepted = QInputDialog.getText(
                self,
                strings.DIALOG_ENTER_PASSWORD_TITLE,
                strings.DIALOG_ENTER_PASSWORD.format(account_id=account_id),
                QLineEdit.EchoMode.Password,
            )
            if not accepted or not password:
                self._status_label.setText(strings.STATUS_CREDENTIAL_REQUIRED)
                return False
            try:
                store.set_password(account_id, password)
            except Exception:
                self._status_label.setText(strings.ERROR_CREDENTIAL_STORE)
                return False
        return True

    def _enabled_account_ids(self) -> tuple[str, ...]:
        create_repository = getattr(self.context, "create_message_repository", None)
        if callable(create_repository):
            try:
                accounts = cast(Any, create_repository()).list_accounts()
            except Exception:
                accounts = ()
            account_ids = tuple(
                account_id
                for account in accounts
                if account.get("is_enabled", 1) not in (False, 0, "0")
                and isinstance(account_id := account.get("id", account.get("account_id")), str)
                and account_id
            )
            if account_ids:
                return account_ids
        return tuple(
            node.account_id
            for root in self.folder_tree_model.roots()
            for node in _walk_folder_tree(root)
            if node.kind == "account" and node.account_id is not None
        )

    def _connect_presentation(self) -> None:
        self.message_list_viewmodel.filters_changed.connect(self.message_table_model.set_filters)
        self.message_list_viewmodel.search_changed.connect(self.message_table_model.set_search)
        self.message_list_viewmodel.request_busy_changed.connect(self._set_query_busy)
        self.message_list_viewmodel.message_selected.connect(self._show_selected_message)
        self.message_list_viewmodel.selection_changed.connect(
            lambda _message_id: self._update_message_actions()
        )
        self.message_list_view.selectionModel().selectionChanged.connect(
            lambda _selected, _deselected: self._update_message_actions()
        )
        self.message_table_model.rowsInserted.connect(self._update_message_count)
        self.message_table_model.modelReset.connect(self._update_message_count)
        self.detail_view.thread_loaded.connect(self.message_table_model.show_thread)
        self.detail_view.attachment_save_requested.connect(self._prepare_attachment_save)
        self.folder_tree_view.selectionModel().currentChanged.connect(
            self._folder_selection_changed
        )
        self.sync_worker.progress.connect(self._show_sync_progress)
        self.sync_worker.sync_result.connect(self._show_sync_result)
        self.sync_worker.folders_refreshed.connect(self._show_folder_refresh_result)
        self.sync_worker.folder_tree_updated.connect(self._update_folder_tree)
        self.sync_worker.error_reported.connect(self._show_sync_error)
        self.sync_worker.file_result.connect(self._show_file_result)
        self.query_worker.result.connect(self._show_export_list_result)
        self.query_worker.request_failed.connect(self._show_export_list_failure)
        self.query_worker.request_cancelled.connect(self._show_export_list_cancelled)
        self.sync_worker.trash_result.connect(self._show_trash_result)
        self.sync_worker.purge_result.connect(self._show_purge_result)
        self.sync_worker.delete_dry_run_result.connect(self._show_delete_dry_run_result)
        self.sync_worker.remote_delete_result.connect(self._show_remote_delete_result)
        self.sync_worker.failures_for_review_result.connect(self._show_failure_review_result)
        self.sync_worker.failure_action_result.connect(self._show_failure_action_result)
        self.verify_worker.verify_result.connect(self._refresh_after_integrity_result)

        self._cancel_button.clicked.connect(self._cancel_current_operation)
        self.message_list_viewmodel.request_page()
        load_folder_tree = getattr(self.sync_worker, "load_folder_tree", None)
        if callable(load_folder_tree):
            load_folder_tree()
        self._update_message_actions()

    def _setup_tray(self) -> None:
        """Create the tray menu when the desktop environment supports it."""

        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray = QSystemTrayIcon(self)
        tray.setIcon(QApplication.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
        tray.setToolTip(f"{strings.APP_NAME}: {strings.TRAY_STATUS_WAITING}")
        tray_menu = QMenu(self)
        open_action = tray_menu.addAction(strings.TRAY_OPEN)
        self._tray_sync_action = tray_menu.addAction(strings.TRAY_SYNC)
        tray_menu.addSeparator()
        quit_action = tray_menu.addAction(strings.TRAY_QUIT)
        open_action.triggered.connect(self._show_from_tray)
        self._tray_sync_action.triggered.connect(self._sync_selected_account)
        quit_action.triggered.connect(self._request_exit)
        tray.setContextMenu(tray_menu)
        tray.activated.connect(self._tray_activated)
        tray.show()
        self._tray_icon = tray

    def _show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            self._show_from_tray()

    def _request_exit(self) -> None:
        self._exit_requested = True
        self.close()

    def _set_tray_status(self, status: str, pixmap: QStyle.StandardPixmap) -> None:
        if self._tray_icon is None:
            return
        self._tray_icon.setIcon(QApplication.style().standardIcon(pixmap))
        self._tray_icon.setToolTip(f"{strings.APP_NAME}: {status}")

    def _start_scheduled_sync(self) -> None:
        if (
            self._storage_write_gate.state is not StorageState.ATTACHED
            or self._sync_token is not None
            or self._folder_refresh_token is not None
            or self.verify_worker.active_tokens
        ):
            return
        self.start_startup_sync()

    def _configure_sync_timer(self, settings: config.AppConfig) -> None:
        self.sync_timer.setInterval(settings.sync_interval_minutes * 60 * 1000)
        if settings.sync_interval_minutes > 0:
            if not self.sync_timer.isActive():
                self.sync_timer.start()
        else:
            self.sync_timer.stop()

    def _update_message_count(self, *_args: object) -> None:
        self._count_label.setText(
            strings.STATUS_MESSAGE_COUNT.format(count=self.message_table_model.rowCount())
        )

    def _refresh_after_integrity_result(self, result: object) -> None:
        if not isinstance(result, ReindexResult):
            return
        self.message_table_model.reload()
        self.sync_worker.load_folder_tree()

    def _set_query_busy(self, busy: bool) -> None:
        self._query_busy = busy
        if busy and self._sync_token is None:
            self._status_label.setText(strings.STATUS_QUERYING)
        elif not busy and self._sync_token is None:
            self._status_label.setText(strings.STATUS_READY)
        self.search_bar.cancel_button.setEnabled(busy)

    def _show_selected_message(self, message_id: object) -> None:
        if not isinstance(message_id, int):
            self.detail_view.clear_message()
            return
        summary = next(
            (item for item in self.message_table_model.items if item.id == message_id),
            None,
        )
        self.detail_view.show_message(summary)

    def _folder_selection_changed(self, current: Any, previous: Any) -> None:
        del previous
        if self._workers_stopped:
            return
        selected_filter = self.folder_tree_model.filter_for_index(current)
        if selected_filter is not None:
            self.message_list_viewmodel.set_filters(selected_filter)
        self._update_message_actions()

    def _selected_message_ids(self) -> tuple[int, ...]:
        selection_model = self.message_list_view.selectionModel()
        if selection_model is None:
            return ()
        items = self.message_table_model.items
        ids = {
            items[index.row()].id
            for index in selection_model.selectedRows()
            if 0 <= index.row() < len(items)
        }
        if ids:
            return tuple(sorted(ids))
        selected_id = self.message_list_viewmodel.selected_message_id
        return (selected_id,) if selected_id is not None else ()

    def _update_message_actions(self) -> None:
        self._update_trash_actions()
        self._update_remote_delete_action()

    def _update_trash_actions(self) -> None:
        selected_id = self.message_list_viewmodel.selected_message_id
        in_trash = self.message_list_viewmodel.filters.local_states == frozenset({"trashed"})
        selected_summary = next(
            (item for item in self.message_table_model.items if item.id == selected_id),
            None,
        )
        enabled = (
            selected_summary is not None
            and selected_summary.local_state == "trashed"
            and in_trash
            and self._storage_write_gate.is_write_allowed()
        )
        self.restore_trash_action.setEnabled(enabled)
        self.purge_trash_action.setEnabled(enabled)

    def _remote_delete_mode(self) -> str:
        configured = getattr(getattr(self.context, "settings", None), "remote_delete_mode", "trash")
        return "expunge" if configured in {"expunge", "permanent"} else "trash"

    def _remote_trash_folder_is_known(self) -> bool:
        settings = getattr(self.context, "settings", None)
        configured = getattr(settings, "remote_trash_folder", None)
        detected = getattr(self.context, "remote_trash_folder", None)
        return any(
            isinstance(value, str) and bool(value.strip()) for value in (configured, detected)
        )

    def _update_remote_delete_action(self) -> None:
        selected = bool(self._selected_message_ids())
        if self._storage_write_gate.state is not StorageState.ATTACHED:
            enabled = False
            reason = strings.REMOTE_DELETE_DISABLED_STORAGE
        elif self._file_token is not None:
            enabled = False
            reason = strings.REMOTE_DELETE_DISABLED_BUSY
        elif not selected:
            enabled = False
            reason = strings.REMOTE_DELETE_DISABLED_NO_SELECTION
        elif self._remote_delete_mode() == "trash" and not self._remote_trash_folder_is_known():
            enabled = False
            reason = strings.REMOTE_DELETE_DISABLED_NO_TRASH
        else:
            enabled = True
            reason = ""
        self.delete_remote_action.setEnabled(enabled)
        self.delete_remote_action.setToolTip(reason)

    def _show_message_list_context_menu(self, position: object) -> None:
        if not isinstance(position, QPoint):
            return
        index = self.message_list_view.indexAt(position)
        selection_model = self.message_list_view.selectionModel()
        if (
            index.isValid()
            and selection_model is not None
            and not selection_model.isSelected(index)
        ):
            selection_model.setCurrentIndex(
                index,
                QItemSelectionModel.SelectionFlag.ClearAndSelect
                | QItemSelectionModel.SelectionFlag.Rows,
            )
        menu = QMenu(self.message_list_view)
        menu.addAction(self.delete_remote_action)
        menu.exec(self.message_list_view.viewport().mapToGlobal(position))

    def _start_remote_delete(self) -> None:
        message_ids = self._selected_message_ids()
        if not message_ids or not self.delete_remote_action.isEnabled():
            return
        self._file_token = self.sync_worker.dry_run_remote_delete(
            message_ids,
            self._storage_write_gate,
        )
        self._status_label.setText(strings.STATUS_REMOTE_DELETE_DRY_RUN)
        self._update_remote_delete_action()

    def _restore_selected_from_trash(self) -> None:
        selected_id = self.message_list_viewmodel.selected_message_id
        if selected_id is None or not self.restore_trash_action.isEnabled():
            return
        self._file_token = self.sync_worker.restore_from_trash((selected_id,))
        self._status_label.setText(strings.STATUS_LOADING)

    def _purge_selected_from_trash(self) -> None:
        selected_id = self.message_list_viewmodel.selected_message_id
        if selected_id is None or not self.purge_trash_action.isEnabled():
            return
        if not ConfirmationDialog(strings.DIALOG_CONFIRM_TRASH_PURGE, self).confirmed():
            return
        self._file_token = self.sync_worker.purge_messages((selected_id,), self._storage_write_gate)
        self._status_label.setText(strings.STATUS_LOADING)

    def _sync_selected_account(self) -> None:
        if (
            self._sync_token is not None
            or self._folder_refresh_token is not None
            or self.verify_worker.active_tokens
        ):
            return
        account_id = self._selected_account_id()
        if account_id is not None:
            if not self._prepare_sync((account_id,)):
                return
            self._start_sync(self.sync_worker.sync_account(account_id))
            return
        # No specific account row is selected (e.g. 'all accounts' or an empty
        # tree): sync every enabled account instead of blocking the user.
        account_ids = self._enabled_account_ids()
        if not account_ids:
            self._status_label.setText(strings.STATUS_NO_ACCOUNT_SELECTED)
            return
        if not self._prepare_sync(account_ids):
            return
        sync_all_accounts = getattr(self.sync_worker, "sync_all_accounts", None)
        if callable(sync_all_accounts):
            token = sync_all_accounts()
            if isinstance(token, CancelToken):
                self._start_sync(token)
            return
        self._start_sync(self.sync_worker.sync_account(account_ids[0]))

    def _refresh_selected_account(self) -> None:
        if (
            self._sync_token is not None
            or self._folder_refresh_token is not None
            or self.verify_worker.active_tokens
        ):
            return
        account_id = self._selected_account_id()
        if account_id is None:
            self._status_label.setText(strings.STATUS_NO_ACCOUNT_SELECTED)
            return
        self._folder_refresh_token = self.sync_worker.refresh_folders(account_id)
        self.refresh_folders_action.setEnabled(False)
        self._status_label.setText(strings.STATUS_REFRESHING_FOLDERS)

    def _start_sync(self, token: CancelToken) -> None:
        self._sync_token = token
        self.sync_action.setEnabled(False)
        if self._tray_sync_action is not None:
            self._tray_sync_action.setEnabled(False)
        self._cancel_button.setEnabled(True)
        self._status_label.setText(strings.STATUS_SYNCING)
        self._set_tray_status(strings.TRAY_STATUS_SYNCING, QStyle.StandardPixmap.SP_ArrowRight)

    def _selected_account_id(self) -> str | None:
        current = self.folder_tree_view.currentIndex()
        node = self.folder_tree_model.data(current, self.folder_tree_model.NodeRole)
        account_id = getattr(node, "account_id", None)
        return account_id if isinstance(account_id, str) else None

    def _show_sync_progress(self, progress: object) -> None:
        if isinstance(progress, (ExportMboxProgress, ExportAttachmentsProgress)):
            self._active_export_count = progress.exported_count
            total = progress.total_count
            processed = progress.processed_count
            self._progress_bar.setValue(min(100, int(processed * 100 / total)) if total else 100)
            self._status_label.setText(f"{strings.EXPORT_STATUS_RUNNING} {processed} / {total}")
            return
        if not isinstance(progress, SyncProgress):
            return
        if progress.total_bytes_estimate > 0:
            self._progress_bar.setValue(
                min(100, int(progress.transferred_bytes * 100 / progress.total_bytes_estimate))
            )
        eta = (
            strings.STATUS_SYNC_ETA_SECONDS.format(seconds=progress.eta_seconds)
            if progress.eta_seconds is not None
            else strings.STATUS_SYNC_ETA_UNKNOWN
        )
        self._status_label.setText(
            strings.STATUS_SYNC_PROGRESS.format(
                folder=progress.current_folder,
                transferred=progress.transferred_bytes,
                total=progress.total_bytes_estimate,
                count=progress.message_count,
                eta=eta,
            )
        )

    def _show_sync_result(self, result: object) -> None:
        if isinstance(result, SyncResult):
            self._sync_token = None
            self.sync_action.setEnabled(True)
            if self._tray_sync_action is not None:
                self._tray_sync_action.setEnabled(True)
            self._cancel_button.setEnabled(False)
            self.message_list_viewmodel.cancel_search()
            self._status_label.setText(
                (
                    strings.STATUS_SYNC_CANCELLED
                    if result.cancelled
                    else strings.STATUS_SYNC_RESULT
                ).format(
                    fetched=result.fetched_count,
                    skipped=result.skipped_count,
                    failed=result.failed_count,
                )
            )
            self._progress_bar.setValue(0)
            self._set_tray_status(
                strings.TRAY_STATUS_WAITING,
                QStyle.StandardPixmap.SP_ComputerIcon,
            )
            self.message_table_model.reload()

    def _show_folder_refresh_result(self, _result: object) -> None:
        self._folder_refresh_token = None
        self.refresh_folders_action.setEnabled(True)
        if not isinstance(_result, FolderRefreshResult):
            self._status_label.setText(strings.STATUS_READY)
            return
        self._status_label.setText(
            strings.STATUS_FOLDER_REFRESH_RESULT.format(
                new_count=_result.new_count,
                removed_count=len(_result.removed_raw_names),
            )
        )

    def _update_folder_tree(self, snapshot: object) -> None:
        if not isinstance(snapshot, FolderTreeSnapshot):
            return
        previous_key = self._current_folder_tree_key()
        self.folder_tree_model.set_roots(
            build_mail_account_roots(snapshot.accounts, snapshot.folders)
        )
        # set_roots() resets the view, collapsing every branch and clearing selection.
        self.folder_tree_view.expandAll()
        self._restore_folder_tree_selection(previous_key)

    def _current_folder_tree_key(self) -> str | None:
        node = self.folder_tree_model.data(
            self.folder_tree_view.currentIndex(),
            self.folder_tree_model.NodeRole,
        )
        key = getattr(node, "key", None)
        return key if isinstance(key, str) else None

    def _restore_folder_tree_selection(self, previous_key: str | None) -> None:
        index = QModelIndex()
        if previous_key is not None:
            index = self.folder_tree_model.index_for_key(previous_key)
        if not index.isValid():
            index = self._default_folder_tree_index()
        if not index.isValid():
            return
        selection_model = self.folder_tree_view.selectionModel()
        if selection_model is not None:
            selection_model.setCurrentIndex(
                index,
                QItemSelectionModel.SelectionFlag.ClearAndSelect
                | QItemSelectionModel.SelectionFlag.Rows,
            )

    def _default_folder_tree_index(self) -> QModelIndex:
        """Prefer the sole account, otherwise fall back to 'all accounts'."""

        all_accounts_key: str | None = None
        account_keys: list[str] = []
        for root in self.folder_tree_model.roots():
            for node in _walk_folder_tree(root):
                if node.kind == "account":
                    account_keys.append(node.key)
                elif node.kind == "all_accounts" and all_accounts_key is None:
                    all_accounts_key = node.key
        if len(account_keys) == 1:
            return self.folder_tree_model.index_for_key(account_keys[0])
        if all_accounts_key is not None:
            return self.folder_tree_model.index_for_key(all_accounts_key)
        return QModelIndex()

    def _show_sync_error(self, notification: object) -> None:
        if not isinstance(notification, SyncErrorNotification):
            return
        if notification.operation == "sync":
            self._sync_token = None
            self.sync_action.setEnabled(True)
            if self._tray_sync_action is not None:
                self._tray_sync_action.setEnabled(True)
            self._cancel_button.setEnabled(False)
            self._set_tray_status(
                strings.TRAY_STATUS_ERROR,
                QStyle.StandardPixmap.SP_MessageBoxWarning,
            )
            self._status_label.setText(notification.message)
            return
        if notification.operation == "refresh_folders":
            self._folder_refresh_token = None
            self.refresh_folders_action.setEnabled(True)
            self._status_label.setText(notification.message)
        elif notification.operation in {
            "prepare_attachment",
            "save_attachment",
            "export_eml",
            "export_mbox",
            "export_attachments",
        }:
            self._file_token = None
            self._pending_attachment_request = None
            self._pending_attachment_plan = None
            self._active_export_kind = None
            self._active_export_count = 0
            self._status_label.setText(notification.message)
        elif notification.operation in {
            "restore_from_trash",
            "purge",
            "remote_delete_dry_run",
            "remote_delete",
        }:
            self._file_token = None
            self._status_label.setText(notification.message)
            self._update_message_actions()
        elif notification.operation in {"failures_for_review", "force_fetch", "reparse"}:
            self._file_token = None
            self._status_label.setText(notification.message)

    def _build_verify_worker(self) -> VerifyWorker:
        manifest_reader_factory = getattr(self.context, "create_manifest_reader_all", None)
        return VerifyWorker(
            self.context.create_message_repository,
            cast(Callable[[], BaseIntegrityStorage], self.context.create_eml_storage),
            manifest_reader_factory if callable(manifest_reader_factory) else None,
            manifest_root=self.context.storage_root,
            reindex_coordinator=getattr(self.context, "rebuild_database", None),
            exclusive_write_guard=self._ensure_verify_write_exclusive,
            connection_manager=self.context.connection_manager,
            operation_gate=self._operation_gate,
        )

    def _ensure_verify_write_exclusive(self) -> None:
        if self._sync_token is not None or self.sync_worker.active_tokens:
            raise RuntimeError(strings.INTEGRITY_SYNC_BUSY)

    def _show_integrity_dialog(self) -> None:
        if self._verify_dialog is not None:
            self._verify_dialog.raise_()
            self._verify_dialog.activateWindow()
            return
        dialog = IntegrityDialog(self.verify_worker, parent=self)
        dialog.operation_finished.connect(self._integrity_finished)
        dialog.finished.connect(self._integrity_dialog_closed)
        self._verify_dialog = dialog
        dialog.show()

    def _show_failure_review(self) -> None:
        if self._failure_review_dialog is not None:
            self._failure_review_dialog.raise_()
            self._failure_review_dialog.activateWindow()
            return
        if self._file_token is not None:
            return
        self._file_token = self.sync_worker.list_failures_for_review()
        self._status_label.setText(strings.FAILURE_REVIEW_STATUS_LOADING)

    def _show_failure_review_result(self, failures: object) -> None:
        self._file_token = None
        if not isinstance(failures, (tuple, list)):
            return
        if not failures:
            self._status_label.setText(strings.FAILURE_REVIEW_STATUS_NONE)
        dialog = FailureReviewDialog(failures, self)
        dialog.fetch_requested.connect(self._force_fetch_failure)
        dialog.reparse_requested.connect(self._reparse_failure)
        dialog.finished.connect(self._failure_review_dialog_closed)
        self._failure_review_dialog = dialog
        dialog.show()

    def _failure_review_dialog_closed(self, _result: int) -> None:
        self._failure_review_dialog = None

    def _show_audit_log(self) -> None:
        if self._audit_log_dialog is not None:
            self._audit_log_dialog.raise_()
            self._audit_log_dialog.activateWindow()
            return
        dialog = AuditLogDialog(self.sync_worker, parent=self)
        dialog.finished.connect(self._audit_log_dialog_closed)
        self._audit_log_dialog = dialog
        dialog.show()

    def _audit_log_dialog_closed(self, _result: int) -> None:
        self._audit_log_dialog = None

    def _force_fetch_failure(self, failure: object) -> None:
        if not isinstance(failure, Mapping):
            self._status_label.setText(strings.FAILURE_REVIEW_INVALID_MESSAGE)
            return
        if self._failure_review_dialog is not None:
            self._failure_review_dialog.close()
        self._file_token = self.sync_worker.force_fetch_message(failure)
        self._status_label.setText(strings.FAILURE_REVIEW_STATUS_FETCHING)

    def _reparse_failure(self, failure: object) -> None:
        if not isinstance(failure, Mapping) or type(failure.get("message_id")) is not int:
            self._status_label.setText(strings.FAILURE_REVIEW_INVALID_MESSAGE)
            return
        if self._failure_review_dialog is not None:
            self._failure_review_dialog.close()
        account_id = failure.get("account_id")
        self._file_token = self.sync_worker.reparse_messages(
            account_id=account_id if isinstance(account_id, str) else None,
            message_ids=(failure["message_id"],),
        )
        self._status_label.setText(strings.FAILURE_REVIEW_STATUS_REPARSING)

    def _show_failure_action_result(self, result: object) -> None:
        self._file_token = None
        if isinstance(result, SyncResult):
            self._status_label.setText(strings.FAILURE_REVIEW_STATUS_FETCH_COMPLETE)
        else:
            self._status_label.setText(strings.FAILURE_REVIEW_STATUS_REPARSE_COMPLETE)
        self.message_table_model.reload()

    def _integrity_finished(self) -> None:
        if self._verify_dialog is not None and not self._verify_dialog.isVisible():
            self._verify_dialog = None

    def _integrity_dialog_closed(self, _result: int) -> None:
        self._verify_dialog = None

    def _prepare_attachment_save(self, request: object) -> None:
        if not isinstance(request, AttachmentSaveRequest):
            return
        detail = request.detail
        if detail.relative_path is None or detail.file_hash is None:
            self._status_label.setText(strings.ERROR_STORAGE)
            return
        selected = QFileDialog.getExistingDirectory(self, strings.SAVE_DIALOG_TITLE)
        if not selected:
            return
        self._pending_attachment_request = request
        self._pending_attachment_plan = None
        self._file_token = self.sync_worker.prepare_attachment_save(
            relative_path=detail.relative_path,
            expected_hash=detail.file_hash,
            part_index=request.part_index,
            dest_dir=Path(selected),
            filename=request.filename,
        )
        self._status_label.setText(strings.STATUS_LOADING)

    def _show_file_result(self, result: object) -> None:
        if isinstance(result, AttachmentSavePlan):
            self._handle_attachment_plan(result)
        elif isinstance(result, SavedFile):
            self._file_token = None
            self._pending_attachment_request = None
            self._pending_attachment_plan = None
            self._status_label.setText(strings.SAVE_SUCCESS.format(filename=result.path.name))
        elif isinstance(result, Path):
            export_kind = self._active_export_kind
            export_count = self._active_export_count
            self._file_token = None
            self._status_label.setText(
                strings.EXPORT_STATUS_MBOX_COMPLETE.format(count=export_count)
                if export_kind == "mbox"
                else strings.SAVE_SUCCESS.format(filename=result.name)
            )
            self._active_export_kind = None
            self._active_export_count = 0
            self._cancel_button.setEnabled(False)
        elif isinstance(result, ExportAttachmentsResult):
            self._file_token = None
            self._active_export_kind = None
            self._cancel_button.setEnabled(False)
            self._status_label.setText(
                strings.EXPORT_STATUS_ATTACHMENTS_COMPLETE.format(
                    count=result.exported_count,
                    skipped=result.skipped_count,
                )
            )

    def _show_trash_result(self, result: object) -> None:
        from mail_dock.usecases.trash import TrashResult

        if not isinstance(result, TrashResult):
            return
        self._file_token = None
        self.message_list_viewmodel.select_message(None)
        self.message_table_model.reload()
        self._status_label.setText(
            strings.STATUS_TRASH_RESTORED if result.restored_ids else strings.STATUS_READY
        )

    def _show_purge_result(self, result: object) -> None:
        from mail_dock.usecases.trash import PurgeResult

        if not isinstance(result, PurgeResult):
            return
        self._file_token = None
        self.message_list_viewmodel.select_message(None)
        self.message_table_model.reload()
        self._status_label.setText(
            strings.STATUS_TRASH_PURGED if result.purged_ids else strings.STATUS_READY
        )

    def _show_delete_dry_run_result(self, result: object) -> None:
        if not isinstance(result, DeleteDryRunResult):
            return
        self._file_token = None
        self._update_remote_delete_action()
        if not result.candidates:
            self._status_label.setText(strings.STATUS_REMOTE_DELETE_NO_CANDIDATES)
            if result.exclusions:
                DeleteDryRunDialog(result, self).exec()
            return
        self._status_label.setText(
            strings.STATUS_REMOTE_DELETE_READY.format(
                count=result.candidate_count,
                size=result.total_size_bytes,
            )
        )
        if DeleteDryRunDialog(result, self).exec() != QDialog.DialogCode.Accepted:
            return
        if DeleteConfirmationDialog(result, self).exec() != QDialog.DialogCode.Accepted:
            return
        settings = getattr(self.context, "settings", None)
        batch_limit = getattr(settings, "delete_batch_limit", 1000)
        self._file_token = self.sync_worker.execute_remote_delete(
            result,
            self._storage_write_gate,
            mode=self._remote_delete_mode(),
            delete_batch_limit=batch_limit,
        )
        self._status_label.setText(strings.STATUS_LOADING)
        self._update_remote_delete_action()

    def _show_remote_delete_result(self, result: object) -> None:
        if not isinstance(result, DeleteResult):
            return
        self._file_token = None
        self.message_list_viewmodel.select_message(None)
        self.message_table_model.reload()
        self._status_label.setText(
            strings.STATUS_REMOTE_DELETE_RESULT.format(
                completed=result.completed_count,
                uncertain=result.uncertain_count,
                skipped=result.skipped_count,
            )
        )
        self._update_message_actions()

    def _handle_attachment_plan(self, plan: AttachmentSavePlan) -> None:
        request = self._pending_attachment_request
        if request is None:
            return
        self._pending_attachment_plan = plan
        name_was_changed = any(warning != "executable_extension" for warning in plan.warnings)
        if (
            name_was_changed
            and not ConfirmationDialog(
                strings.SAVE_ATTACHMENT_SANITIZED.format(
                    original=request.filename or "attachment",
                    sanitized=plan.filename,
                ),
                self,
            ).confirmed()
        ):
            self._pending_attachment_request = None
            self._pending_attachment_plan = None
            self._file_token = None
            return
        if plan.is_executable and not confirm_save_executable(plan.filename, self):
            self._pending_attachment_request = None
            self._pending_attachment_plan = None
            self._file_token = None
            return
        self._file_token = self.sync_worker.commit_attachment_save(plan)

    def _export_current_message(self) -> None:
        detail = self.detail_view.current_detail
        if not isinstance(detail, MessageDetail):
            self._status_label.setText(strings.ERROR_STORAGE)
            return
        if detail.relative_path is None or detail.file_hash is None:
            self._status_label.setText(strings.ERROR_STORAGE)
            return
        selected, _filter = QFileDialog.getSaveFileName(
            self,
            strings.SAVE_DIALOG_TITLE,
            strings.EXPORT_DEFAULT_FILENAME,
            "EML files (*.eml)",
        )
        if not selected:
            return
        destination = Path(selected)
        if destination.exists() and not confirm_overwrite(destination.name, self):
            return
        self._file_token = self.sync_worker.export_eml(
            relative_path=detail.relative_path,
            expected_hash=detail.file_hash,
            dest_path=destination,
        )
        self._status_label.setText(strings.STATUS_LOADING)

    def _export_mbox(self) -> None:
        self._begin_export("mbox")

    def _export_attachments(self) -> None:
        self._begin_export("attachments")

    def _begin_export(self, kind: str) -> None:
        if self._file_token is not None or self._export_list_token is not None:
            return
        message_ids = self._choose_export_message_ids()
        if message_ids is None:
            return
        if kind == "mbox":
            selected, _filter = QFileDialog.getSaveFileName(
                self,
                strings.EXPORT_MBOX_DIALOG_TITLE,
                strings.EXPORT_MBOX_DEFAULT_FILENAME,
                "mbox files (*.mbox);;All files (*)",
            )
            destination = Path(selected) if selected else None
        else:
            selected = QFileDialog.getExistingDirectory(
                self,
                strings.EXPORT_ATTACHMENTS_DIALOG_TITLE,
            )
            destination = Path(selected) if selected else None
        if destination is None:
            return
        self._pending_export_kind = kind
        self._pending_export_destination = destination
        if message_ids:
            self._start_export(kind, destination, message_ids)
            return
        handle = self.query_worker.list_all_messages(
            query=self.message_list_viewmodel.query,
            mode=self.message_list_viewmodel.mode,
            filters=self.message_list_viewmodel.filters,
        )
        self._export_list_token = handle.token
        self._cancel_button.setEnabled(True)
        self._status_label.setText(strings.EXPORT_STATUS_LOADING_LIST)

    def _choose_export_message_ids(self) -> tuple[int, ...] | None:
        selected = self._selected_message_ids()
        if not selected:
            return ()
        dialog = QMessageBox(self)
        dialog.setWindowTitle(strings.EXPORT_SOURCE_TITLE)
        dialog.setText(strings.EXPORT_SOURCE_TITLE)
        selected_button = dialog.addButton(
            strings.EXPORT_SOURCE_SELECTED,
            QMessageBox.ButtonRole.AcceptRole,
        )
        current_button = dialog.addButton(
            strings.EXPORT_SOURCE_CURRENT_LIST,
            QMessageBox.ButtonRole.DestructiveRole,
        )
        dialog.addButton(QMessageBox.StandardButton.Cancel)
        dialog.exec()
        if dialog.clickedButton() is selected_button:
            return selected
        if dialog.clickedButton() is current_button:
            return ()
        return None

    def _start_export(self, kind: str, destination: Path, message_ids: tuple[int, ...]) -> None:
        if not message_ids:
            self._clear_pending_export()
            self._status_label.setText(strings.EXPORT_STATUS_NO_MESSAGES)
            return
        self._active_export_kind = kind
        self._active_export_count = 0
        if kind == "mbox":
            self._file_token = self.sync_worker.export_mbox(
                message_ids=message_ids,
                dest_path=destination,
            )
        else:
            self._file_token = self.sync_worker.export_attachments(
                message_ids=message_ids,
                dest_dir=destination,
            )
        self._cancel_button.setEnabled(True)
        self._status_label.setText(strings.EXPORT_STATUS_RUNNING)

    def _show_export_list_result(self, result: object) -> None:
        if getattr(result, "channel", None) != "export/list":
            return
        if self._export_list_token is None:
            return
        self._export_list_token = None
        value = getattr(result, "value", ())
        message_ids = tuple(
            item.id for item in value if hasattr(item, "id") and type(item.id) is int
        )
        kind = self._pending_export_kind
        destination = self._pending_export_destination
        self._clear_pending_export()
        if kind is not None and destination is not None:
            self._start_export(kind, destination, message_ids)

    def _show_export_list_failure(self, failure: object) -> None:
        if getattr(failure, "channel", None) != "export/list":
            return
        self._export_list_token = None
        self._clear_pending_export()
        self._cancel_button.setEnabled(False)
        self._status_label.setText(
            user_message(cast(BaseException, getattr(failure, "error", failure)))
        )

    def _show_export_list_cancelled(self, cancelled: object) -> None:
        if getattr(cancelled, "channel", None) != "export/list":
            return
        self._export_list_token = None
        self._clear_pending_export()
        self._cancel_button.setEnabled(False)

    def _clear_pending_export(self) -> None:
        self._pending_export_kind = None
        self._pending_export_destination = None

    def _show_storage_detached(self, _error: object) -> None:
        """Show the detached banner; recovery state handling remains Phase 4."""

        self._storage_detached_banner.setText(strings.BANNER_STORAGE_DETACHED)
        self._storage_detached_banner.setVisible(True)
        self._storage_status_label.setText(strings.ERROR_STORAGE_DETACHED)
        self.sync_action.setEnabled(False)
        tray_sync_action = getattr(self, "_tray_sync_action", None)
        if tray_sync_action is not None:
            tray_sync_action.setEnabled(False)
        set_tray_status = getattr(self, "_set_tray_status", None)
        if callable(set_tray_status):
            set_tray_status(
                strings.TRAY_STATUS_DETACHED,
                QStyle.StandardPixmap.SP_MessageBoxCritical,
            )
        self.refresh_folders_action.setEnabled(False)
        storage_detach_action = getattr(self, "storage_detach_action", None)
        if storage_detach_action is not None:
            storage_detach_action.setEnabled(False)

    def _show_storage_detached_by_user(self) -> None:
        self._storage_detached_banner.setText(strings.BANNER_STORAGE_DETACHED_BY_USER)
        self._storage_detached_banner.setVisible(True)
        self._storage_status_label.setText(strings.STATUS_STORAGE_DETACHED_BY_USER)
        self.sync_action.setEnabled(False)
        tray_sync_action = getattr(self, "_tray_sync_action", None)
        if tray_sync_action is not None:
            tray_sync_action.setEnabled(False)
        set_tray_status = getattr(self, "_set_tray_status", None)
        if callable(set_tray_status):
            set_tray_status(
                strings.TRAY_STATUS_DETACHED,
                QStyle.StandardPixmap.SP_MessageBoxCritical,
            )
        self.refresh_folders_action.setEnabled(False)
        storage_detach_action = getattr(self, "storage_detach_action", None)
        if storage_detach_action is not None:
            storage_detach_action.setEnabled(False)

    def set_storage_state(self, state: object) -> None:
        """Reflect the monitor's lifecycle state in the main window."""

        if isinstance(state, StorageState):
            self._storage_write_gate.state = state
        self._update_trash_actions()

        if state in {StorageState.DETACHED, StorageState.DETACHED_BY_USER}:
            if state is StorageState.DETACHED_BY_USER:
                self._show_storage_detached_by_user()
            else:
                self._show_storage_detached(state)
            return
        if state is StorageState.ATTACHED:
            self._storage_detached_banner.setVisible(False)
            self._storage_status_label.setText(strings.STATUS_STORAGE_CONNECTED)
            self.sync_action.setEnabled(self._sync_token is None)
            if self._tray_sync_action is not None:
                self._tray_sync_action.setEnabled(self._sync_token is None)
            self.refresh_folders_action.setEnabled(self._folder_refresh_token is None)
            self._set_tray_status(
                strings.TRAY_STATUS_WAITING,
                QStyle.StandardPixmap.SP_ComputerIcon,
            )
            self.storage_detach_action.setEnabled(self._on_storage_detach is not None)

    def _show_settings(self) -> None:
        dialog = SettingsDialog(self.context, self)
        dialog.settings_saved.connect(self._settings_changed)
        # Account add/edit persists immediately, independent of the dialog's own OK/Cancel.
        dialog.accounts_changed.connect(self.sync_worker.load_folder_tree)
        dialog.exec()

    def _settings_changed(self, settings: object) -> None:
        if not isinstance(settings, config.AppConfig):
            return
        self.sync_worker.set_sync_options(
            SyncOptions(
                max_message_bytes=settings.max_message_bytes,
                flag_refresh_enabled=settings.flag_refresh_enabled,
                flag_refresh_window_days=settings.flag_refresh_window_days,
                flag_refresh_min_interval_seconds=settings.flag_refresh_min_interval_seconds,
            )
        )
        self.detail_view.set_block_remote_images(settings.block_remote_images)
        self.message_table_model.set_trash_grace_days(settings.trash_grace_days)
        self._configure_sync_timer(settings)
        root_uuid = getattr(self.context, "root_uuid", None)
        raw_profile = (
            settings.storage_profiles.get(root_uuid) if isinstance(root_uuid, str) else None
        )
        profile = raw_profile if isinstance(raw_profile, dict) else {}
        self.set_storage_encryption(
            profile.get("encryption", getattr(self.context, "encryption_declaration", "unknown"))
        )
        self.set_storage_capability(getattr(self.context, "capability_level", None))
        self._set_credential_storage_status(settings.credential_storage)
        self.sync_worker.load_folder_tree()

    def _open_log_folder(self) -> None:
        path = config.config_dir() / "logs"
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _open_encryption_guide(self) -> None:
        guide = Path(__file__).resolve().parents[4] / "README.md"
        if guide.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(guide)))

    def _cancel_current_operation(self) -> None:
        if self._sync_token is not None:
            self._sync_token.cancel()
        if self._file_token is not None:
            self._file_token.cancel()
        if self._export_list_token is not None:
            self._export_list_token.cancel()

    def _restore_ui_state(self) -> None:
        geometry = self._ui_settings.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        splitter_state = self._ui_settings.value("splitter")
        if splitter_state is not None:
            self.splitter.restoreState(splitter_state)
        header_state = self._ui_settings.value("message_header")
        if header_state is not None:
            self.message_list_view.horizontalHeader().restoreState(header_state)

    def _save_ui_state(self) -> None:
        self._ui_settings.setValue("geometry", self.saveGeometry())
        self._ui_settings.setValue("splitter", self.splitter.saveState())
        self._ui_settings.setValue(
            "message_header",
            self.message_list_view.horizontalHeader().saveState(),
        )


def _walk_folder_tree(node: Any) -> tuple[Any, ...]:
    """Flatten folder-tree nodes for startup credential preflight."""

    nodes = [node]
    for child in getattr(node, "children", ()):
        nodes.extend(_walk_folder_tree(child))
    return tuple(nodes)
