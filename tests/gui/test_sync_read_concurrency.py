from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, cast

import pytest

from mail_dock.domain.fetcher import BaseMailFetcher
from mail_dock.domain.ports import BaseEmlStorage, BaseManifestWriter, BaseMessageRenderer
from mail_dock.domain.repository import BaseMessageRepository, MessageRecord
from mail_dock.domain.search import BaseSearchRepository
from mail_dock.infrastructure.database.connection import ConnectionManager
from mail_dock.presentation.threads.query_worker import QueryResult, QueryWorker
from mail_dock.presentation.threads.sync_worker import SyncWorker
from mail_dock.usecases.sync_mail import SyncResult

pytestmark = pytest.mark.gui


class _Fetcher:
    def __enter__(self) -> _Fetcher:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _Repository:
    def list_accounts(self) -> list[dict[str, object]]:
        return [{"id": "account-1"}]


def _wait_for_value(qtbot: Any, values: list[object], expected: object) -> None:
    qtbot.waitUntil(
        lambda: any(isinstance(value, QueryResult) and value.value == expected for value in values),
        timeout=2_000,
    )


def test_sync_does_not_block_list_search_or_open_and_uses_another_connection(
    tmp_path: Path,
    qtbot: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ConnectionManager(tmp_path / "metadata.db")
    connection_ids: dict[str, int] = {}
    sync_started = threading.Event()
    release_sync = threading.Event()
    query_results: list[object] = []

    def sync_repository_factory() -> BaseMessageRepository:
        connection_ids["sync"] = id(manager.get_connection())
        return cast(BaseMessageRepository, _Repository())

    def query_repository_factory() -> BaseSearchRepository:
        connection_ids["query"] = id(manager.get_connection())
        return cast(BaseSearchRepository, object())

    def fetcher_factory(_account: MessageRecord) -> BaseMailFetcher:
        return cast(BaseMailFetcher, _Fetcher())

    def storage_factory() -> BaseEmlStorage:
        return cast(BaseEmlStorage, object())

    def manifest_factory(_account_id: str) -> BaseManifestWriter:
        return cast(BaseManifestWriter, object())

    def renderer_factory() -> BaseMessageRenderer:
        return cast(BaseMessageRenderer, object())

    def blocking_sync(*_args: Any, **_kwargs: Any) -> SyncResult:
        sync_started.set()
        if not release_sync.wait(timeout=2):
            raise AssertionError("sync test was not released")
        return SyncResult(0, 0, 0, 0, False)

    def list_stub(*_args: Any, **_kwargs: Any) -> str:
        return "list"

    def search_stub(*_args: Any, **_kwargs: Any) -> str:
        return "search"

    def open_stub(*_args: Any, **_kwargs: Any) -> str:
        return "open"

    monkeypatch.setattr(
        "mail_dock.presentation.threads.query_worker.list_messages",
        list_stub,
    )
    monkeypatch.setattr(
        "mail_dock.presentation.threads.query_worker.search_messages",
        search_stub,
    )

    sync_worker = SyncWorker(
        sync_repository_factory,
        fetcher_factory,
        storage_factory,
        manifest_factory,
        sync_account_usecase=blocking_sync,
        connection_manager=manager,
    )
    query_worker = QueryWorker(
        query_repository_factory,
        storage_factory=storage_factory,
        renderer_factory=renderer_factory,
        open_message_usecase=open_stub,
        connection_manager=manager,
    )
    sync_worker.start()
    query_worker.start()
    query_worker.result.connect(query_results.append)

    try:
        sync_worker.sync_account("account-1")
        qtbot.waitUntil(sync_started.is_set, timeout=2_000)

        query_worker.list_messages()
        _wait_for_value(qtbot, query_results, "list")

        query_worker.search_messages(query="term")
        _wait_for_value(qtbot, query_results, "search")

        query_worker.open_message(message_id=1)
        _wait_for_value(qtbot, query_results, "open")

        assert connection_ids["sync"] != connection_ids["query"]
    finally:
        release_sync.set()
        sync_worker.stop()
        query_worker.stop()
        manager.request_close_all()
        manager.assert_all_closed()
