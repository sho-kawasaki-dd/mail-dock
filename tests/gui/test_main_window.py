from __future__ import annotations

from typing import Any, cast

import pytest
from PySide6.QtWidgets import QSplitter

from mail_dock import config
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.ports import BaseEmlStorage, BaseManifestWriter
from mail_dock.domain.repository import BaseMessageRepository
from mail_dock.domain.search import BaseSearchRepository, MessageFilter, SearchPage
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
    def __init__(self) -> None:
        self.settings = config.AppConfig()
        self.connection_manager = None
        self.folder_tree_roots: tuple[object, ...] = ()
        self.stop_calls = 0

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
