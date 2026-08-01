"""Main application window and presentation composition for the GUI shell."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
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

from mail_dock.presentation import strings
from mail_dock.presentation.context import AppContext
from mail_dock.presentation.models.folder_tree_model import FolderTreeModel
from mail_dock.presentation.models.message_table_model import MessageTableModel
from mail_dock.presentation.threads.query_worker import QueryWorker
from mail_dock.presentation.threads.sync_worker import SyncWorker
from mail_dock.presentation.viewmodels.message_list_viewmodel import MessageListViewModel
from mail_dock.presentation.views.detail_view import DetailView
from mail_dock.presentation.views.message_list import MessageListSearchBar, MessageListView
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
        """Start synchronization for the selected account when one exists."""

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
        self.setCentralWidget(self.splitter)

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
        self.refresh_folders_action.triggered.connect(self.refresh_folders_requested)
        self.settings_action.triggered.connect(self.settings_requested)
        self.export_eml_action.triggered.connect(self.export_requested)
        self.thread_view_action.triggered.connect(self.detail_view.request_thread)
        self.exit_action.triggered.connect(self.close)

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
        self.folder_tree_view.selectionModel().currentChanged.connect(
            self._folder_selection_changed
        )
        self.sync_worker.progress.connect(self._show_sync_progress)
        self.sync_worker.sync_result.connect(self._show_sync_result)
        self.sync_worker.folders_refreshed.connect(self._show_folder_refresh_result)
        self.sync_worker.storage_detached.connect(self._show_storage_detached)

        self._cancel_button.clicked.connect(self._cancel_current_operation)
        self.message_list_viewmodel.request_page()

    def _update_message_count(self, *_args: object) -> None:
        self._count_label.setText(
            strings.STATUS_MESSAGE_COUNT.format(count=self.message_table_model.rowCount())
        )

    def _set_query_busy(self, busy: bool) -> None:
        if busy:
            self._status_label.setText(strings.STATUS_QUERYING)
        elif not self.sync_worker.active_tokens:
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
        account_id = self._selected_account_id()
        if account_id is None:
            self._status_label.setText(strings.STATUS_NO_ACCOUNT_SELECTED)
            return
        self.sync_worker.sync_account(account_id)

    def _selected_account_id(self) -> str | None:
        current = self.folder_tree_view.currentIndex()
        node = self.folder_tree_model.data(current, self.folder_tree_model.NodeRole)
        account_id = getattr(node, "account_id", None)
        return account_id if isinstance(account_id, str) else None

    def _show_sync_progress(self, progress: object) -> None:
        if not isinstance(progress, SyncProgress):
            return
        self._status_label.setText(
            strings.STATUS_SYNC_PROGRESS.format(
                folder=progress.current_folder,
                transferred=progress.transferred_bytes,
                total=progress.total_bytes_estimate,
            )
        )
        if progress.total_bytes_estimate > 0:
            self._progress_bar.setValue(
                min(100, int(progress.transferred_bytes * 100 / progress.total_bytes_estimate))
            )

    def _show_sync_result(self, result: object) -> None:
        if isinstance(result, SyncResult):
            self._status_label.setText(strings.STATUS_SYNC_COMPLETE)
            self._progress_bar.setValue(0)

    def _show_folder_refresh_result(self, _result: object) -> None:
        self._status_label.setText(strings.STATUS_READY)

    def _show_storage_detached(self, _error: object) -> None:
        self._storage_status_label.setText(strings.ERROR_STORAGE_DETACHED)
        self.sync_action.setEnabled(False)

    def _cancel_current_operation(self) -> None:
        self.message_list_viewmodel.cancel_search()
        self.sync_worker.cancel_all()

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
