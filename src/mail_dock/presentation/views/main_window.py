"""Main application window and presentation composition for the GUI shell."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from PySide6.QtCore import QItemSelectionModel, QModelIndex, QSettings, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStatusBar,
    QToolBar,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from mail_dock import config
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.messages import AttachmentSavePlan, SavedFile
from mail_dock.domain.search import MessageDetail
from mail_dock.presentation import strings
from mail_dock.presentation.context import AppContext
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
from mail_dock.presentation.viewmodels.message_list_viewmodel import MessageListViewModel
from mail_dock.presentation.views.detail_view import AttachmentSaveRequest, DetailView
from mail_dock.presentation.views.dialogs.confirmation_dialog import (
    ConfirmationDialog,
    confirm_overwrite,
    confirm_save_executable,
)
from mail_dock.presentation.views.dialogs.settings_dialog import SettingsDialog
from mail_dock.presentation.views.message_list import MessageListSearchBar, MessageListView
from mail_dock.usecases.sync_folders import FolderRefreshResult
from mail_dock.usecases.sync_mail import SyncOptions, SyncProgress, SyncResult


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
    ) -> None:
        super().__init__()
        self.context = context
        self._on_storage_root_switch = on_storage_root_switch
        self._on_storage_setup = on_storage_setup
        self.setObjectName("mainWindow")
        self.setWindowTitle(strings.MAIN_WINDOW_TITLE)
        self._workers_stopped = False
        self._sync_token: CancelToken | None = None
        self._folder_refresh_token: CancelToken | None = None
        self._file_token: CancelToken | None = None
        self._pending_attachment_request: AttachmentSaveRequest | None = None
        self._pending_attachment_plan: AttachmentSavePlan | None = None
        self._query_busy = False
        self._ui_settings = QSettings("mail-dock", "mail-dock")

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
            sync_options=SyncOptions(max_message_bytes=context.settings.max_message_bytes),
            connection_manager=context.connection_manager,
        )
        self.query_worker.start()
        self.sync_worker.start()

        query_worker = cast(Any, self.query_worker)
        self.message_list_viewmodel = MessageListViewModel(query_worker, self)
        self.message_table_model = MessageTableModel(
            query_worker,
            self,
            page_size=MessageListViewModel.DEFAULT_PAGE_SIZE,
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
        self.context.stop_workers()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        """Persist the UI shell and stop workers before the window closes."""

        self._save_ui_state()
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
        self.thread_view_action = QAction(strings.MAIN_MENU_THREAD_VIEW, self)
        self.exit_action = QAction(strings.MAIN_MENU_EXIT, self)
        self.open_log_folder_action = QAction(strings.MAIN_MENU_OPEN_LOG_FOLDER, self)
        self.storage_info_action = QAction(strings.MAIN_MENU_STORAGE_INFO, self)
        self.storage_switch_action = QAction(strings.MAIN_MENU_STORAGE_SWITCH, self)
        self.storage_setup_action = QAction(strings.MAIN_MENU_STORAGE_SETUP, self)

        toolbar = QToolBar(strings.APP_NAME, self)
        toolbar.setObjectName("mainToolBar")
        toolbar.addAction(self.sync_action)
        toolbar.addAction(self.refresh_folders_action)
        toolbar.addSeparator()
        toolbar.addWidget(self.search_bar)
        toolbar.addSeparator()
        toolbar.addAction(self.settings_action)
        self.addToolBar(toolbar)

        file_menu = self.menuBar().addMenu(strings.MAIN_MENU_FILE)
        file_menu.addAction(self.export_eml_action)
        file_menu.addSeparator()
        file_menu.addAction(self.exit_action)
        view_menu = self.menuBar().addMenu(strings.MAIN_MENU_VIEW)
        view_menu.addAction(self.thread_view_action)
        storage_menu = self.menuBar().addMenu(strings.MAIN_MENU_STORAGE)
        storage_menu.addAction(self.storage_info_action)
        storage_menu.addAction(self.storage_switch_action)
        storage_menu.addAction(self.storage_setup_action)
        help_menu = self.menuBar().addMenu(strings.MAIN_MENU_HELP)
        help_menu.addAction(self.open_log_folder_action)
        self.encryption_help_action = QAction(strings.MAIN_MENU_HELP_ENCRYPTION, self)
        help_menu.addAction(self.encryption_help_action)

        self.sync_action.triggered.connect(self._sync_selected_account)
        self.refresh_folders_action.triggered.connect(self._refresh_selected_account)
        self.settings_action.triggered.connect(self.settings_requested)
        self.export_eml_action.triggered.connect(self._export_current_message)
        self.thread_view_action.triggered.connect(self.detail_view.request_thread)
        self.exit_action.triggered.connect(self.close)
        self.settings_requested.connect(self._show_settings)
        self.open_log_folder_action.triggered.connect(self._open_log_folder)
        self.encryption_help_action.triggered.connect(self._open_encryption_guide)
        self.storage_info_action.triggered.connect(self._show_storage_root)
        self.storage_switch_action.triggered.connect(self._request_storage_switch)
        self.storage_setup_action.triggered.connect(self._request_storage_setup)

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
                self._query_busy,
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
        if self.has_active_operations() and not ConfirmationDialog(
            strings.DIALOG_CONFIRM_STORAGE_SWITCH_BUSY,
            self,
        ).confirmed():
            return
        self._on_storage_root_switch(Path(selected))

    def _request_storage_setup(self) -> None:
        if self._on_storage_setup is None:
            return
        if self.has_active_operations() and not ConfirmationDialog(
            strings.DIALOG_CONFIRM_STORAGE_SETUP_BUSY,
            self,
        ).confirmed():
            return
        self._on_storage_setup(None)

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
        self.sync_worker.storage_detached.connect(self._show_storage_detached)
        self.sync_worker.file_result.connect(self._show_file_result)
        self.query_worker.storage_detached.connect(self._show_storage_detached)

        self._cancel_button.clicked.connect(self._cancel_current_operation)
        self.message_list_viewmodel.request_page()
        load_folder_tree = getattr(self.sync_worker, "load_folder_tree", None)
        if callable(load_folder_tree):
            load_folder_tree()

    def _update_message_count(self, *_args: object) -> None:
        self._count_label.setText(
            strings.STATUS_MESSAGE_COUNT.format(count=self.message_table_model.rowCount())
        )

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
        selected_filter = self.folder_tree_model.filter_for_index(current)
        if selected_filter is not None:
            self.message_list_viewmodel.set_filters(selected_filter)

    def _sync_selected_account(self) -> None:
        if self._sync_token is not None or self._folder_refresh_token is not None:
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
        if self._sync_token is not None or self._folder_refresh_token is not None:
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
        self._cancel_button.setEnabled(True)
        self._status_label.setText(strings.STATUS_SYNCING)

    def _selected_account_id(self) -> str | None:
        current = self.folder_tree_view.currentIndex()
        node = self.folder_tree_model.data(current, self.folder_tree_model.NodeRole)
        account_id = getattr(node, "account_id", None)
        return account_id if isinstance(account_id, str) else None

    def _show_sync_progress(self, progress: object) -> None:
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
        if notification.operation == "refresh_folders":
            self._folder_refresh_token = None
            self.refresh_folders_action.setEnabled(True)
            self._status_label.setText(notification.message)
        elif notification.operation in {
            "prepare_attachment",
            "save_attachment",
            "export_eml",
        }:
            self._file_token = None
            self._pending_attachment_request = None
            self._pending_attachment_plan = None
            self._status_label.setText(notification.message)

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
            self._file_token = None
            self._status_label.setText(strings.SAVE_SUCCESS.format(filename=result.name))

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

    def _show_storage_detached(self, _error: object) -> None:
        """Show the detached banner; recovery state handling remains Phase 4."""

        self._storage_detached_banner.setText(strings.BANNER_STORAGE_DETACHED)
        self._storage_detached_banner.setVisible(True)
        self._storage_status_label.setText(strings.ERROR_STORAGE_DETACHED)
        self.sync_action.setEnabled(False)
        self.refresh_folders_action.setEnabled(False)

    def _show_settings(self) -> None:
        dialog = SettingsDialog(self.context, self)
        dialog.settings_saved.connect(self._settings_changed)
        dialog.exec()

    def _settings_changed(self, settings: object) -> None:
        if not isinstance(settings, config.AppConfig):
            return
        self.sync_worker.set_sync_options(SyncOptions(max_message_bytes=settings.max_message_bytes))
        self.detail_view.set_block_remote_images(settings.block_remote_images)
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
