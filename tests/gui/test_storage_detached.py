from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QLabel, QWidget

from mail_dock.domain.errors import StorageDetachedError
from mail_dock.domain.fetcher import BaseMailFetcher
from mail_dock.domain.ports import BaseEmlStorage, BaseManifestWriter
from mail_dock.domain.repository import BaseMessageRepository, MessageRecord
from mail_dock.domain.search import BaseSearchRepository
from mail_dock.presentation import strings
from mail_dock.presentation.threads.query_worker import QueryWorker
from mail_dock.presentation.threads.sync_worker import SyncWorker
from mail_dock.presentation.views.main_window import MainWindow
from mail_dock.usecases.sync_mail import SyncResult

pytestmark = pytest.mark.gui


class _Fetcher:
    def __enter__(self) -> _Fetcher:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _SyncRepository:
    def list_accounts(self) -> list[dict[str, object]]:
        return [{"id": "account-1"}]


def test_query_worker_propagates_storage_detached(
    qtbot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_list(*_args: Any, **_kwargs: Any) -> object:
        raise StorageDetachedError("detached")

    monkeypatch.setattr(
        "mail_dock.presentation.threads.query_worker.list_messages",
        failing_list,
    )
    worker = QueryWorker(lambda: cast(BaseSearchRepository, object()))
    detached: list[object] = []
    worker.storage_detached.connect(detached.append)
    worker.start()

    try:
        worker.list_messages()
        qtbot.waitUntil(lambda: bool(detached), timeout=2_000)
        assert isinstance(detached[0], StorageDetachedError)
    finally:
        worker.stop()


def test_sync_worker_propagates_storage_detached(qtbot: Any) -> None:
    def repository_factory() -> BaseMessageRepository:
        return cast(BaseMessageRepository, _SyncRepository())

    def fetcher_factory(_account: MessageRecord) -> BaseMailFetcher:
        return cast(BaseMailFetcher, _Fetcher())

    def storage_factory() -> BaseEmlStorage:
        return cast(BaseEmlStorage, object())

    def manifest_factory(_account_id: str) -> BaseManifestWriter:
        return cast(BaseManifestWriter, object())

    def failing_sync(*_args: Any, **_kwargs: Any) -> SyncResult:
        raise StorageDetachedError("detached")

    worker = SyncWorker(
        repository_factory,
        fetcher_factory,
        storage_factory,
        manifest_factory,
        sync_account_usecase=failing_sync,
    )
    detached: list[object] = []
    worker.storage_detached.connect(detached.append)
    worker.start()

    try:
        worker.sync_account("account-1")
        qtbot.waitUntil(lambda: bool(detached), timeout=2_000)
        assert isinstance(detached[0], StorageDetachedError)
    finally:
        worker.stop()


def test_main_window_shows_detached_banner_and_disables_sync(qtbot: Any) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    banner = QLabel(parent)
    status_label = QLabel(parent)
    sync_action = QAction(parent)
    refresh_folders_action = QAction(parent)
    parent.show()

    MainWindow._show_storage_detached(
        cast(
            MainWindow,
            SimpleNamespace(
                _storage_detached_banner=banner,
                _storage_status_label=status_label,
                sync_action=sync_action,
                refresh_folders_action=refresh_folders_action,
            ),
        ),
        StorageDetachedError("detached"),
    )

    assert banner.isVisible()
    assert banner.text() == strings.BANNER_STORAGE_DETACHED
    assert status_label.text() != ""
    assert not sync_action.isEnabled()
    assert not refresh_folders_action.isEnabled()
