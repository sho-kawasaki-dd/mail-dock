from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from PySide6.QtWidgets import QSplitter

from mail_dock import config
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.ports import BaseEmlStorage, BaseManifestWriter
from mail_dock.domain.repository import BaseMessageRepository
from mail_dock.domain.search import BaseSearchRepository, MessageFilter, SearchPage
from mail_dock.presentation import strings
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


class _Context:
    def __init__(self, *, profile: dict[str, config.JSONValue] | None = None) -> None:
        self.storage_root: Path | None = None
        self.root_uuid = "root-1" if profile is not None else None
        self.settings = config.AppConfig(
            storage_root_uuid=self.root_uuid,
            storage_profiles={self.root_uuid: profile} if self.root_uuid else {},
        )
        self.connection_manager = None
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


def test_storage_menu_and_status_bar_display_active_root(qtbot: Any) -> None:
    context = _Context(profile={"encryption": "encrypted", "capability_level": "ok"})
    context.storage_root = Path("C:/mail-dock")
    window = MainWindow(cast(Any, context))
    qtbot.addWidget(window)

    assert window.storage_info_action.text() == strings.MAIN_MENU_STORAGE_INFO
    assert window.storage_switch_action.text() == strings.MAIN_MENU_STORAGE_SWITCH
    assert window.storage_setup_action.text() == strings.MAIN_MENU_STORAGE_SETUP
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
