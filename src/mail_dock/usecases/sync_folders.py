"""Use cases for refreshing remote folders and choosing sync targets."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from mail_dock.domain.fetcher import BaseMailFetcher
from mail_dock.domain.repository import BaseMessageRepository

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class FolderRefreshResult:
    """Describe folders added by, or missing from, a remote refresh."""

    new_count: int
    removed_raw_names: tuple[str, ...]


def refresh_folders(
    fetcher: BaseMailFetcher,
    repo: BaseMessageRepository,
    account_id: str,
) -> FolderRefreshResult:
    """Refresh folder metadata without enabling newly discovered folders."""

    existing = {str(folder["raw_name"]): folder for folder in repo.list_folders(account_id)}
    remote_folders = fetcher.list_folders()
    remote_raw_names: set[str] = set()
    new_count = 0

    for remote_folder in remote_folders:
        raw_name = remote_folder.raw_name
        remote_raw_names.add(raw_name)
        folder_record: dict[str, object] = {
            "account_id": account_id,
            "raw_name": raw_name,
            "display_name": remote_folder.display_name,
        }
        if raw_name not in existing:
            folder_record.update(
                {
                    "uidvalidity": remote_folder.uidvalidity,
                    "is_sync_target": 0,
                }
            )
            new_count += 1
        repo.upsert_folder(folder_record)

    removed_raw_names = tuple(raw_name for raw_name in existing if raw_name not in remote_raw_names)
    for raw_name in removed_raw_names:
        _LOGGER.warning(
            "Remote folder is no longer available: account=%s folder=%s",
            account_id,
            raw_name,
        )

    return FolderRefreshResult(
        new_count=new_count,
        removed_raw_names=removed_raw_names,
    )


def set_sync_target(
    repo: BaseMessageRepository,
    account_id: str,
    raw_name: str,
    enabled: bool,
) -> None:
    """Enable or disable synchronization for one known folder."""

    repo.set_sync_target(account_id, raw_name, enabled)
