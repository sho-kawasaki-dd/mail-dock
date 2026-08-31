from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from PySide6.QtCore import QItemSelectionModel, QObject, Signal

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
from mail_dock.presentation import strings
from mail_dock.presentation.models.message_table_model import (
    MessageQueryWorker,
    MessageTableModel,
)
from mail_dock.presentation.views.main_window import MainWindow

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


class _CredentialStore:
    def get_password(self, _account_id: str) -> str | None:
        return "stored-password"

    def set_password(self, _account_id: str, _password: str) -> None:
        return None


class _Context:
    def __init__(self, *, trash_grace_days: int = 30) -> None:
        self.storage_root: Path | None = None
        self.root_uuid: str | None = None
        self.settings = config.AppConfig(trash_grace_days=trash_grace_days)
        self.connection_manager = None
        self.remote_trash_folder: str | None = None
        self.encryption_declaration = "unknown"
        self.capability_level = None
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

    @staticmethod
    def create_remote_image_detector() -> Any:
        return lambda _html: False

    def stop_workers(self) -> None:
        self.stop_calls += 1

    def save_settings(self, settings: config.AppConfig) -> None:
        self.saved.append(settings)
        self.settings = settings


def _summary(
    message_id: int = 1,
    *,
    local_state: str = "active",
    trashed_at: datetime | None = None,
) -> MessageSummary:
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
        local_state=local_state,
        thread_key=f"thread-{message_id}",
        imap_flags="\\Seen",
        moved_to_folder_display_name=None,
        failure_class=None,
        flags_seen_at=None,
        trashed_at=trashed_at,
    )


def test_selecting_trash_node_switches_message_filter_to_trashed(qtbot: Any) -> None:
    window = MainWindow(cast(Any, _Context()))
    qtbot.addWidget(window)
    # The folder tree is populated asynchronously by a background worker, and
    # set_roots() resets the model (invalidating any index captured earlier),
    # so the root index must be recomputed fresh on every poll. With one
    # account the root has 3 children: all-accounts, the account, and trash.
    qtbot.waitUntil(
        lambda: window.folder_tree_model.rowCount(window.folder_tree_model.index(0, 0)) >= 3,
        timeout=2_000,
    )

    root = window.folder_tree_model.index(0, 0)
    trash_index = window.folder_tree_model.index(
        window.folder_tree_model.rowCount(root) - 1, 0, root
    )
    assert window.folder_tree_model.data(trash_index) == strings.TREE_LOCAL_TRASH

    selection_model = window.folder_tree_view.selectionModel()
    assert selection_model is not None
    selection_model.setCurrentIndex(trash_index, QItemSelectionModel.SelectionFlag.ClearAndSelect)

    assert window.message_list_viewmodel.filters == MessageFilter(
        local_states=frozenset({"trashed"})
    )
    window.stop_workers()


class _FakeWorker(QObject):
    result = Signal(object)


def test_remaining_days_column_shows_countdown_only_for_trashed_messages(qtbot: Any) -> None:
    worker = _FakeWorker()
    model = MessageTableModel(cast(MessageQueryWorker, worker), trash_grace_days=30)

    trashed_recently = _summary(
        1, local_state="trashed", trashed_at=datetime.now(UTC) - timedelta(days=1)
    )
    active = _summary(2)
    model.show_thread((trashed_recently, active))

    # The trash countdown is always the last column (see _HEADERS in message_table_model.py).
    remaining_column = model.columnCount() - 1
    trashed_value = model.data(model.index(0, remaining_column))
    active_value = model.data(model.index(1, remaining_column))

    assert trashed_value == strings.TRASH_REMAINING_DAYS.format(days=29)
    assert active_value == ""


def test_restore_action_enabled_only_in_trash_view_for_trashed_selection_and_dispatches(
    qtbot: Any,
) -> None:
    window = MainWindow(cast(Any, _Context()))
    qtbot.addWidget(window)
    window.message_table_model.show_thread((_summary(1, local_state="trashed"),))
    selection_model = window.message_list_view.selectionModel()
    assert selection_model is not None
    selection_model.setCurrentIndex(
        window.message_table_model.index(0, 0),
        QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows,
    )
    window._update_message_actions()

    # Not viewing the trash filter yet: restore/purge stay disabled.
    assert not window.restore_trash_action.isEnabled()
    assert not window.purge_trash_action.isEnabled()

    # Switching into the trash filter resets the table model's rows (see
    # MessageTableModel.set_filters), so the trashed row must be re-injected
    # afterwards via show_thread rather than relying on the (stubbed, empty)
    # search repository to repopulate it.
    window.message_list_viewmodel.set_filters(MessageFilter(local_states=frozenset({"trashed"})))
    window.message_table_model.show_thread((_summary(1, local_state="trashed"),))
    selection_model.setCurrentIndex(
        window.message_table_model.index(0, 0),
        QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows,
    )
    window._update_message_actions()
    assert window.restore_trash_action.isEnabled()
    assert window.purge_trash_action.isEnabled()

    calls: list[tuple[int, ...]] = []

    def restore_from_trash(message_ids: tuple[int, ...]) -> CancelToken:
        calls.append(message_ids)
        return CancelToken()

    cast(Any, window.sync_worker).restore_from_trash = restore_from_trash

    window._restore_selected_from_trash()

    assert calls == [(1,)]
    window.stop_workers()


def test_purge_action_requires_confirmation_before_dispatching(
    qtbot: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = MainWindow(cast(Any, _Context()))
    qtbot.addWidget(window)
    window.message_list_viewmodel.set_filters(MessageFilter(local_states=frozenset({"trashed"})))
    window.message_table_model.show_thread((_summary(1, local_state="trashed"),))
    selection_model = window.message_list_view.selectionModel()
    assert selection_model is not None
    selection_model.setCurrentIndex(
        window.message_table_model.index(0, 0),
        QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows,
    )
    window._update_message_actions()

    calls: list[tuple[int, ...]] = []

    def purge_messages(message_ids: tuple[int, ...], _gate: object) -> CancelToken:
        calls.append(message_ids)
        return CancelToken()

    cast(Any, window.sync_worker).purge_messages = purge_messages

    class _Rejecting:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def confirmed(self) -> bool:
            return False

    monkeypatch.setattr(
        "mail_dock.presentation.views.main_window.ConfirmationDialog",
        _Rejecting,
    )
    window._purge_selected_from_trash()
    assert calls == []

    class _Accepting(_Rejecting):
        def confirmed(self) -> bool:
            return True

    monkeypatch.setattr(
        "mail_dock.presentation.views.main_window.ConfirmationDialog",
        _Accepting,
    )
    window._purge_selected_from_trash()
    assert calls == [(1,)]
    window.stop_workers()
