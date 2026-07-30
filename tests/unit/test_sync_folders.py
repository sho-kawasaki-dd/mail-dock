from __future__ import annotations

import logging

from mail_dock.domain.fetcher import RemoteFolder
from mail_dock.usecases.sync_folders import refresh_folders, set_sync_target
from tests.support.fake_fetcher import FakeFetcher
from tests.support.in_memory_repository import InMemoryMessageRepository


def test_refresh_folders_adds_new_folders_disabled_and_updates_display_name() -> None:
    repository = InMemoryMessageRepository()
    repository.upsert_folder(
        {
            "account_id": "account",
            "raw_name": "INBOX",
            "display_name": "Old inbox label",
            "is_sync_target": 1,
        }
    )
    fetcher = FakeFetcher(
        folders=(
            RemoteFolder("INBOX", "受信トレイ", uidvalidity=7),
            RemoteFolder("Archive", "アーカイブ", uidvalidity=8),
        )
    )

    result = refresh_folders(fetcher, repository, "account")

    assert result.new_count == 1
    assert result.removed_raw_names == ()
    folders = {str(folder["raw_name"]): folder for folder in repository.list_folders("account")}
    assert folders["INBOX"]["display_name"] == "受信トレイ"
    assert folders["INBOX"]["is_sync_target"] == 1
    assert folders["Archive"]["display_name"] == "アーカイブ"
    assert folders["Archive"]["is_sync_target"] == 0


def test_refresh_folders_keeps_missing_folders_and_reports_them(caplog) -> None:  # type: ignore[no-untyped-def]
    repository = InMemoryMessageRepository()
    repository.upsert_folder(
        {
            "account_id": "account",
            "raw_name": "Removed",
            "display_name": "Removed folder",
            "is_sync_target": 1,
        }
    )
    fetcher = FakeFetcher(folders=(RemoteFolder("INBOX", "Inbox"),))

    with caplog.at_level(logging.WARNING):
        result = refresh_folders(fetcher, repository, "account")

    assert result.new_count == 1
    assert result.removed_raw_names == ("Removed",)
    assert len(repository.list_folders("account")) == 2
    assert "folder=Removed" in caplog.text


def test_set_sync_target_delegates_to_repository() -> None:
    repository = InMemoryMessageRepository()
    repository.upsert_folder({"account_id": "account", "raw_name": "INBOX"})

    set_sync_target(repository, "account", "INBOX", True)

    assert repository.list_sync_targets("account")[0]["raw_name"] == "INBOX"