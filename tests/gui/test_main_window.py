from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from PySide6.QtCore import QItemSelectionModel
from PySide6.QtWidgets import QSplitter

from mail_dock import config
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.ports import BaseEmlStorage, BaseManifestWriter
from mail_dock.domain.repository import BaseMessageRepository
from mail_dock.domain.search import (
    BaseSearchRepository,
    MessageFilter,
    MessageSummary,
    SearchPage,
)
from mail_dock.domain.storage_state import StorageState
from mail_dock.presentation import strings
from mail_dock.presentation.views.main_window import MainWindow
from mail_dock.usecases.delete_remote import DeleteResult
from mail_dock.usecases.export_mbox import ExportMboxProgress

pytestmark = pytest.mark.gui


class _Repository:
    def list_accounts(self) -> list[dict[str, object]]:
        return [{"id": "account-1", "display_name": "仕事"}]

    def list_folders(self, account_id: str) -> list[dict[str, object]]:
        return [
            {
                "id": 10,
                "account_id": account_id,
                "raw_name": "INBOX",
                "display_name": "受信箱",
                "is_sync_target": 1,
            }
        ]


class _SearchRepository:
    def list_messages(
        self,
        filters: MessageFilter,
        *,
        cursor: object = None,
        limit: int = 200,
        cancel: object = None,
    ) -> SearchPage:
        del filters, cursor, limit, cancel
        return SearchPage((), None, True)

    def search_messages(self, *args: object, **kwargs: object) -> SearchPage:
        del args, kwargs
        return SearchPage((), None, True)

    def count_messages(self, *args: object, **kwargs: object) -> int:
        del args, kwargs
        return 0

    def list_thread(self, *args: object, **kwargs: object) -> tuple[object, ...]:
        del args, kwargs
        return ()

    def get_message(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        return None


class _Fetcher:
    def __enter__(self) -> _Fetcher:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _Context:
    def __init__(self, *, profile: dict[str, config.JSONValue] | None = None) -> None:
        self.storage_root: Path | None = None
        self.root_uuid = "root-1" if profile is not None else None
        self.settings = config.AppConfig(
            storage_root_uuid=self.root_uuid,
            storage_profiles={self.root_uuid: profile} if self.root_uuid else {},
        )
        self.connection_manager = None
        self.remote_trash_folder: str | None = None
        self.encryption_declaration = (
            profile.get("encryption", "unknown") if profile is not None else "unknown"
        )
        self.capability_level = profile.get("capability_level") if profile is not None else None
        self.credential_storage = "keyring"
        self.credential_store = _CredentialStore()
        self.folder_tree_roots: tuple[object, ...] = ()
        self.stop_calls = 0
        self.saved: list[config.AppConfig] = []

    @staticmethod
    def create_message_repository() -> BaseMessageRepository:
        return cast(BaseMessageRepository, _Repository())

    @staticmethod
    def create_search_repository() -> BaseSearchRepository:
        return cast(BaseSearchRepository, _SearchRepository())

    @staticmethod
    def create_fetcher(_account: dict[str, object]) -> Any:
        return _Fetcher()

    @staticmethod
    def create_eml_storage() -> BaseEmlStorage:
        return cast(BaseEmlStorage, object())

    @staticmethod
    def create_manifest_writer(_account_id: str) -> BaseManifestWriter:
        return cast(BaseManifestWriter, object())

    @staticmethod
    def create_message_renderer() -> Any:
        return object()

    @staticmethod
    def create_html_sanitizer() -> Any:
        return lambda html, **_kwargs: html

    def stop_workers(self) -> None:
        self.stop_calls += 1

    def save_settings(self, settings: config.AppConfig) -> None:
        self.saved.append(settings)
        self.settings = settings


class _CredentialStore:
    def get_password(self, _account_id: str) -> str | None:
        return "stored-password"

    def set_password(self, _account_id: str, _password: str) -> None:
        return None


def _summary(message_id: int = 1) -> MessageSummary:
    return MessageSummary(
        id=message_id,
        account_id="account-1",
        folder_id=10,
        folder_raw_name="INBOX",
        folder_display_name="受信箱",
        subject=f"件名 {message_id}",
        sender="sender@example.com",
        date_sent=datetime(2026, 1, 2, tzinfo=UTC),
        internal_date=None,
        size_bytes=128,
        has_attachment=False,
        remote_state="present",
        local_state="active",
        thread_key=f"thread-{message_id}",
        imap_flags="\\Seen",
        moved_to_folder_display_name=None,
        failure_class=None,
        flags_seen_at=None,
    )


def test_main_window_builds_three_panes_and_prevents_sync_reentry(qtbot: Any) -> None:
    context = _Context()
    window = MainWindow(cast(Any, context))
    qtbot.addWidget(window)
    window.show()

    assert isinstance(window.splitter, QSplitter)
    assert window.splitter.count() == 3
    assert window.folder_tree_view.model() is window.folder_tree_model
    assert window.message_list_view.model() is window.message_table_model

    calls: list[bool] = []

    def sync_all_accounts() -> CancelToken:
        calls.append(True)
        return CancelToken()

    cast(Any, window.sync_worker).sync_all_accounts = sync_all_accounts
    window.start_startup_sync()
    window.start_startup_sync()

    assert calls == [True]
    window.stop_workers()


def test_stop_workers_is_idempotent_and_waits_for_worker_shutdown(qtbot: Any) -> None:
    context = _Context()
    window = MainWindow(cast(Any, context))
    qtbot.addWidget(window)

    window.stop_workers()
    window.stop_workers()

    assert context.stop_calls == 1
    assert window._workers_stopped


def test_status_bar_displays_encryption_and_capability_state(qtbot: Any) -> None:
    context = _Context(profile={"encryption": "unknown", "capability_level": "degraded"})
    window = MainWindow(cast(Any, context))
    qtbot.addWidget(window)

    assert "unknown" in window._encryption_status_label.text()
    assert "DEGRADED" in window._storage_status_label.text()
    assert window.encryption_help_action.text() == "保管先の暗号化について"
    window.stop_workers()


def test_remote_delete_requires_attached_storage_trash_and_selection(qtbot: Any) -> None:
    context = _Context(profile={"encryption": "unknown", "capability_level": "ok"})
    context.remote_trash_folder = "Trash"
    window = MainWindow(cast(Any, context))
    qtbot.addWidget(window)
    window.message_table_model.show_thread((_summary(1), _summary(2)))

    assert window.message_list_view.selectionMode().name == "ExtendedSelection"
    assert not window.delete_remote_action.isEnabled()
    assert window.delete_remote_action.toolTip() == strings.REMOTE_DELETE_DISABLED_NO_SELECTION

    selection_model = window.message_list_view.selectionModel()
    assert selection_model is not None
    selection_model.select(
        window.message_table_model.index(0, 0),
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
    )
    selection_model.select(
        window.message_table_model.index(1, 0),
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
    )
    window._update_message_actions()
    assert window.delete_remote_action.isEnabled()

    window._storage_write_gate.state = StorageState.DETACHED
    window._update_message_actions()
    assert not window.delete_remote_action.isEnabled()
    assert window.delete_remote_action.toolTip() == strings.REMOTE_DELETE_DISABLED_STORAGE
    window.stop_workers()


def test_remote_delete_is_disabled_until_trash_folder_is_known(qtbot: Any) -> None:
    context = _Context(profile={"encryption": "unknown", "capability_level": "ok"})
    window = MainWindow(cast(Any, context))
    qtbot.addWidget(window)
    window.message_table_model.show_thread((_summary(),))
    selection_model = window.message_list_view.selectionModel()
    assert selection_model is not None
    selection_model.select(
        window.message_table_model.index(0, 0),
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
    )
    window._update_message_actions()

    assert not window.delete_remote_action.isEnabled()
    assert window.delete_remote_action.toolTip() == strings.REMOTE_DELETE_DISABLED_NO_TRASH
    window.stop_workers()


def test_remote_delete_result_reloads_message_list(
    qtbot: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = MainWindow(cast(Any, _Context()))
    qtbot.addWidget(window)
    reloads: list[bool] = []
    monkeypatch.setattr(window.message_table_model, "reload", lambda: reloads.append(True))

    window._show_remote_delete_result(DeleteResult(completed_ids=(1,)))

    assert reloads == [True]
    assert window._status_label.text() == strings.STATUS_REMOTE_DELETE_RESULT.format(
        completed=1,
        uncertain=0,
        skipped=0,
    )
    window.stop_workers()


def test_mbox_export_dispatches_selected_messages_and_reports_count(
    qtbot: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = MainWindow(cast(Any, _Context()))
    qtbot.addWidget(window)
    monkeypatch.setattr(window, "_choose_export_message_ids", lambda: (7, 8))
    monkeypatch.setattr(
        "mail_dock.presentation.views.main_window.QFileDialog.getSaveFileName",
        lambda *_args, **_kwargs: ("/tmp/messages.mbox", ""),
    )
    calls: list[tuple[tuple[int, ...], Path]] = []

    def export_mbox(*, message_ids: tuple[int, ...], dest_path: Path) -> CancelToken:
        calls.append((message_ids, dest_path))
        return CancelToken()

    cast(Any, window.sync_worker).export_mbox = export_mbox

    window._export_mbox()
    window._show_sync_progress(ExportMboxProgress(2, 2, 2, 0, 8))
    window._show_file_result(Path("/tmp/messages.mbox"))

    assert calls == [((7, 8), Path("/tmp/messages.mbox"))]
    assert window._status_label.text() == strings.EXPORT_STATUS_MBOX_COMPLETE.format(count=2)
    window.stop_workers()


def test_export_current_list_loads_all_messages_before_dispatch(
    qtbot: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = MainWindow(cast(Any, _Context()))
    qtbot.addWidget(window)
    monkeypatch.setattr(window, "_choose_export_message_ids", lambda: ())
    monkeypatch.setattr(
        "mail_dock.presentation.views.main_window.QFileDialog.getSaveFileName",
        lambda *_args, **_kwargs: ("/tmp/messages.mbox", ""),
    )
    list_calls: list[dict[str, object]] = []
    export_calls: list[tuple[int, ...]] = []

    def list_all_messages(**kwargs: object) -> SimpleNamespace:
        list_calls.append(kwargs)
        return SimpleNamespace(token=CancelToken())

    def export_mbox(*, message_ids: tuple[int, ...], dest_path: Path) -> CancelToken:
        del dest_path
        export_calls.append(message_ids)
        return CancelToken()

    cast(Any, window.query_worker).list_all_messages = list_all_messages
    cast(Any, window.sync_worker).export_mbox = export_mbox

    window._begin_export("mbox")
    window._show_export_list_result(
        SimpleNamespace(channel="export/list", value=(_summary(3), _summary(4)))
    )

    assert list_calls == [
        {
            "query": window.message_list_viewmodel.query,
            "mode": window.message_list_viewmodel.mode,
            "filters": window.message_list_viewmodel.filters,
        }
    ]
    assert export_calls == [(3, 4)]
    window.stop_workers()


def test_storage_menu_and_status_bar_display_active_root(qtbot: Any) -> None:
    context = _Context(profile={"encryption": "encrypted", "capability_level": "ok"})
    context.storage_root = Path("C:/mail-dock")
    window = MainWindow(cast(Any, context))
    qtbot.addWidget(window)

    assert window.storage_info_action.text() == strings.MAIN_MENU_STORAGE_INFO
    assert window.storage_switch_action.text() == strings.MAIN_MENU_STORAGE_SWITCH
    assert window.storage_setup_action.text() == strings.MAIN_MENU_STORAGE_SETUP
    assert window.storage_detach_action.text() == strings.MAIN_MENU_STORAGE_DETACH
    assert not window.storage_detach_action.isEnabled()
    assert str(context.storage_root) in window._storage_root_label.text()
    assert window._storage_root_label.toolTip() == str(context.storage_root)
    window.stop_workers()


def test_first_sync_confirmation_is_before_queue_and_only_once(
    qtbot: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _Context(profile={"encryption": "unknown", "capability_level": "ok"})
    events: list[str] = []

    class _Confirmation:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            events.append("confirm")

        def confirmed(self) -> bool:
            return True

    monkeypatch.setattr(
        "mail_dock.presentation.views.main_window.ConfirmationDialog",
        _Confirmation,
    )
    window = MainWindow(cast(Any, context))
    qtbot.addWidget(window)

    def queue_sync() -> CancelToken:
        events.append("queue")
        return CancelToken()

    cast(Any, window.sync_worker).sync_all_accounts = queue_sync
    window.start_startup_sync()
    assert events == ["confirm", "queue"]
    profile = context.saved[-1].storage_profiles["root-1"]
    assert isinstance(profile, dict)
    assert profile.get("first_sync_confirmed_at")

    window._sync_token = None
    window.start_startup_sync()
    assert events == ["confirm", "queue", "queue"]
    window.stop_workers()


def test_session_only_password_cancel_does_not_queue_sync(
    qtbot: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _Context(profile={"encryption": "encrypted", "capability_level": "ok"})
    context.credential_storage = "session_only"

    class _EmptyCredentialStore(_CredentialStore):
        def get_password(self, _account_id: str) -> str | None:
            return None

    context.credential_store = _EmptyCredentialStore()
    monkeypatch.setattr(
        "mail_dock.presentation.views.main_window.QInputDialog.getText",
        lambda *_args, **_kwargs: ("", False),
    )
    window = MainWindow(cast(Any, context))
    qtbot.addWidget(window)
    queued: list[str] = []

    def queue_sync() -> CancelToken:
        queued.append("queue")
        return CancelToken()

    cast(Any, window.sync_worker).sync_all_accounts = queue_sync

    window.start_startup_sync()

    assert queued == []
    assert window._status_label.text() == strings.STATUS_CREDENTIAL_REQUIRED
    window.stop_workers()


@pytest.mark.parametrize(
    ("interval_minutes", "expected_interval", "expected_active"),
    ((0, 0, False), (15, 15 * 60 * 1000, True)),
)
def test_sync_timer_uses_configured_interval(
    qtbot: Any,
    interval_minutes: int,
    expected_interval: int,
    expected_active: bool,
) -> None:
    context = _Context()
    context.settings = config.AppConfig(sync_interval_minutes=interval_minutes)
    window = MainWindow(cast(Any, context))
    qtbot.addWidget(window)

    assert window.sync_timer.interval() == expected_interval
    assert window.sync_timer.isActive() is expected_active
    window.stop_workers()


def test_scheduled_sync_skips_detached_and_running_operations(qtbot: Any) -> None:
    context = _Context()
    context.settings = config.AppConfig(sync_interval_minutes=0)
    window = MainWindow(cast(Any, context))
    qtbot.addWidget(window)
    calls: list[bool] = []

    def queue_sync() -> CancelToken:
        calls.append(True)
        return CancelToken()

    cast(Any, window.sync_worker).sync_all_accounts = queue_sync

    window._start_scheduled_sync()
    window._sync_token = None
    window._storage_write_gate.state = StorageState.DETACHED
    window._start_scheduled_sync()
    window._storage_write_gate.state = StorageState.ATTACHED
    window._sync_token = CancelToken()
    window._start_scheduled_sync()

    assert calls == [True]
    window.stop_workers()


def test_tray_menu_and_close_behavior(qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from PySide6.QtWidgets import QSystemTrayIcon

    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    context = _Context()
    context.settings = config.AppConfig(sync_interval_minutes=0)
    window = MainWindow(cast(Any, context))
    qtbot.addWidget(window)
    window.show()

    assert window._tray_icon is not None
    tray_actions = window._tray_icon.contextMenu().actions()
    assert [action.text() for action in tray_actions if not action.isSeparator()] == [
        strings.TRAY_OPEN,
        strings.TRAY_SYNC,
        strings.TRAY_QUIT,
    ]

    window.close()
    assert not window._workers_stopped
    assert window._tray_icon.isVisible()

    window._request_exit()
    assert window._workers_stopped


def test_tray_unavailable_falls_back_to_closing(
    qtbot: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from PySide6.QtWidgets import QSystemTrayIcon

    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: False))
    window = MainWindow(cast(Any, _Context()))
    qtbot.addWidget(window)

    window.close()

    assert window._workers_stopped


def test_settings_change_reconfigures_sync_timer(qtbot: Any) -> None:
    context = _Context()
    context.settings = config.AppConfig(sync_interval_minutes=0)
    window = MainWindow(cast(Any, context))
    qtbot.addWidget(window)

    window._settings_changed(config.AppConfig(sync_interval_minutes=20))
    assert window.sync_timer.interval() == 20 * 60 * 1000
    assert window.sync_timer.isActive()

    window._settings_changed(config.AppConfig(sync_interval_minutes=0))
    assert not window.sync_timer.isActive()
    window.stop_workers()
