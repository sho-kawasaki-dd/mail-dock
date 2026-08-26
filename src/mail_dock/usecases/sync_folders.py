"""Use cases for refreshing remote folders and choosing sync targets."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from mail_dock.domain.fetcher import BaseMailFetcher
from mail_dock.domain.ports import BaseManifestReader, BaseManifestWriter
from mail_dock.domain.repository import BaseMessageRepository
from mail_dock.usecases.snapshots import record_folder_snapshot

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
    *,
    manifest: BaseManifestWriter | None = None,
    manifest_reader: BaseManifestReader | None = None,
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
            "uidvalidity": remote_folder.uidvalidity,
            "delimiter": remote_folder.delimiter,
            "is_sync_target": int(existing.get(raw_name, {}).get("is_sync_target", 0)),
        }
        if raw_name not in existing:
            folder_record.update(
                {
                    "is_sync_target": 0,
                }
            )
            new_count += 1
        if manifest is not None and manifest_reader is not None:
            record_folder_snapshot(manifest, manifest_reader, account_id, folder_record)
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
    *,
    manifest: BaseManifestWriter | None = None,
    manifest_reader: BaseManifestReader | None = None,
) -> None:
    """Enable or disable synchronization for one known folder."""

    folder = next(
        (item for item in repo.list_folders(account_id) if item.get("raw_name") == raw_name),
        None,
    )
    if folder is None:
        repo.set_sync_target(account_id, raw_name, enabled)
        return
    updated_folder = dict(folder)
    updated_folder["is_sync_target"] = int(enabled)
    if manifest is not None and manifest_reader is not None:
        record_folder_snapshot(manifest, manifest_reader, account_id, updated_folder)
    repo.set_sync_target(account_id, raw_name, enabled)
