"""Main application window and presentation composition for the GUI shell."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from PySide6.QtCore import QSettings, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QFileDialog,
    QLabel,
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

    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self.context = context
        self.setObjectName("mainWindow")
        self.setWindowTitle(strings.MAIN_WINDOW_TITLE)
        self._workers_stopped = False
        self._sync_token: CancelToken | None = None
        self._folder_refresh_token: CancelToken | None = None
        self._file_token: CancelToken | None = None
        self._pending_attachment_request: AttachmentSaveRequest | None = None
        self._pending_attachment_plan: AttachmentSavePlan | None = None
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
        self._connect_presentation()
        self._restore_ui_state()

    def start_startup_sync(self) -> None:
        """Start one synchronization run for all enabled accounts."""

        if self._sync_token is not None:
            return
        sync_all_accounts = getattr(self.sync_worker, "sync_all_accounts", None)
        if callable(sync_all_accounts):
            self._start_sync(sync_all_accounts())
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
        help_menu = self.menuBar().addMenu(strings.MAIN_MENU_HELP)
        help_menu.addAction(self.open_log_folder_action)

        self.sync_action.triggered.connect(self._sync_selected_account)
        self.refresh_folders_action.triggered.connect(self._refresh_selected_account)
        self.settings_action.triggered.connect(self.settings_requested)
        self.export_eml_action.triggered.connect(self._export_current_message)
        self.thread_view_action.triggered.connect(self.detail_view.request_thread)
        self.exit_action.triggered.connect(self.close)
        self.settings_requested.connect(self._show_settings)
        self.open_log_folder_action.triggered.connect(self._open_log_folder)

    def _build_status_bar(self) -> None:
        status = QStatusBar(self)
        self.setStatusBar(status)
        self._status_label = QLabel(strings.STATUS_READY, self)
        self._count_label = QLabel(strings.STATUS_MESSAGE_COUNT.format(count=0), self)
        self._storage_status_label = QLabel(strings.STATUS_STORAGE_CONNECTED, self)
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
        status.addPermanentWidget(self._storage_status_label)

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
        if account_id is None:
            self._status_label.setText(strings.STATUS_NO_ACCOUNT_SELECTED)
            return
        self._start_sync(self.sync_worker.sync_account(account_id))

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
        self.folder_tree_model.set_roots(
            build_mail_account_roots(snapshot.accounts, snapshot.folders)
        )

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
        if name_was_changed and not ConfirmationDialog(
            strings.SAVE_ATTACHMENT_SANITIZED.format(
                original=request.filename or "attachment",
                sanitized=plan.filename,
            ),
            self,
        ).confirmed():
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
        self.sync_worker.load_folder_tree()

    def _open_log_folder(self) -> None:
        path = config.config_dir() / "logs"
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

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
