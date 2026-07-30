from __future__ import annotations

from pathlib import Path

import pytest

import mail_dock.infrastructure.fetchers.onamae_imap as onamae_imap
from mail_dock.domain.fetcher import CancelToken
from mail_dock.infrastructure.storage.eml_storage import EmlStorage
from mail_dock.infrastructure.storage.manifest import ManifestWriter
from mail_dock.infrastructure.storage.storage_root import initialize_root
from mail_dock.usecases.sync_mail import SyncOptions, SyncProgress, sync_account
from tests.support.imap_integration import (
    append_message,
    create_mailbox,
    imap_client,
    make_fetcher,
    open_repository,
    register_account_and_folder,
    service,
    unique_mailbox,
)


@pytest.mark.docker
def test_real_sync_resumes_history_and_new_mail_without_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(onamae_imap, "_FETCH_CHUNK_SIZE", 50)
    settings = service("dovecot")
    mailbox = unique_mailbox("SyncResume")
    with imap_client(settings) as client:
        create_mailbox(client, mailbox)
        for index in range(105):
            append_message(
                client,
                mailbox,
                message_id=f"<resume-{index}@example.test>",
                subject=f"resume {index}",
                body=f"history body {index}",
            )

    account_id = "integration-resume"
    repository, connection = open_repository(tmp_path)
    root = tmp_path / "storage"
    initialize_root(root)
    folder_id = register_account_and_folder(repository, account_id, mailbox)
    storage = EmlStorage(root)
    manifest = ManifestWriter(root, account_id)
    fetcher = make_fetcher(settings)
    first_cancel = CancelToken()

    def cancel_after_first_batch(progress: SyncProgress) -> None:
        if progress.message_count >= 100:
            first_cancel.cancel()

    try:
        fetcher.connect()
        first = sync_account(
            fetcher,
            repository,
            storage,
            manifest,
            account_id=account_id,
            options=SyncOptions(),
            cancel=first_cancel,
            on_progress=cancel_after_first_batch,
        )
        assert first.cancelled is True
        cursor = connection.execute(
            "SELECT backfill_next_uid FROM folders WHERE id = ?", (folder_id,)
        ).fetchone()
        assert cursor == (5,)

        second = sync_account(
            fetcher,
            repository,
            storage,
            manifest,
            account_id=account_id,
            options=SyncOptions(),
            cancel=CancelToken(),
        )
        assert second.cancelled is False
    finally:
        fetcher.disconnect()

    with imap_client(settings) as client:
        for index in range(3):
            append_message(
                client,
                mailbox,
                message_id=f"<new-{index}@example.test>",
                subject=f"new {index}",
                body=f"new body {index}",
            )

    fetcher = make_fetcher(settings)
    new_cancel = CancelToken()

    def cancel_during_new_mail(progress: SyncProgress) -> None:
        if progress.message_count >= 1:
            new_cancel.cancel()

    try:
        fetcher.connect()
        interrupted = sync_account(
            fetcher,
            repository,
            storage,
            manifest,
            account_id=account_id,
            options=SyncOptions(),
            cancel=new_cancel,
            on_progress=cancel_during_new_mail,
        )
        assert interrupted.cancelled is True

        completed = sync_account(
            fetcher,
            repository,
            storage,
            manifest,
            account_id=account_id,
            options=SyncOptions(),
            cancel=CancelToken(),
        )
        assert completed.cancelled is False
    finally:
        fetcher.disconnect()
        manifest.close()

    total_messages = connection.execute(
        "SELECT COUNT(*) FROM messages WHERE account_id = ?", (account_id,)
    ).fetchone()
    assert total_messages == (108,)
    distinct_source_keys = connection.execute(
        "SELECT COUNT(DISTINCT source_item_key) FROM messages WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    assert distinct_source_keys == (108,)
    final_cursor = connection.execute(
        "SELECT last_seen_uid, backfill_next_uid, initial_sync_completed FROM folders WHERE id = ?",
        (folder_id,),
    ).fetchone()
    assert final_cursor == (108, 0, 1)
