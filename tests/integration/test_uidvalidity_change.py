from __future__ import annotations

from pathlib import Path

import pytest

from mail_dock.domain.fetcher import CancelToken
from mail_dock.infrastructure.storage.eml_storage import EmlStorage
from mail_dock.infrastructure.storage.manifest import ManifestWriter
from mail_dock.infrastructure.storage.storage_root import initialize_root
from mail_dock.usecases.sync_mail import SyncOptions, sync_account
from tests.support.dovecot_uidvalidity import force_uidvalidity_change_in_container
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
def test_uidvalidity_change_keeps_old_rows_and_reuses_eml(tmp_path: Path) -> None:
    settings = service("dovecot")
    mailbox = unique_mailbox("UIDValidity")
    with imap_client(settings) as client:
        create_mailbox(client, mailbox)
        append_message(
            client,
            mailbox,
            message_id="<uidvalidity@example.test>",
            body="same content across UID generations",
        )

    account_id = "integration-uidvalidity"
    repository, connection = open_repository(tmp_path)
    root = tmp_path / "storage"
    initialize_root(root)
    folder_id = register_account_and_folder(repository, account_id, mailbox)
    storage = EmlStorage(root)
    manifest = ManifestWriter(root, account_id)
    fetcher = make_fetcher(settings)
    try:
        fetcher.connect()
        first_uidvalidity = fetcher.select_folder(mailbox)
        sync_account(
            fetcher,
            repository,
            storage,
            manifest,
            account_id=account_id,
            options=SyncOptions(),
            cancel=CancelToken(),
        )
    finally:
        fetcher.disconnect()

    repository.record_failure(
        account_id,
        folder_id,
        first_uidvalidity,
        1,
        "transient",
        "preserve this old-generation failure",
    )
    force_uidvalidity_change_in_container(
        Path("tests/docker/compose.yaml"),
        mailbox_path=f"/var/mail/vmail/testuser/Maildir/.{mailbox}",
    )
    second_fetcher = make_fetcher(settings)
    try:
        second_fetcher.connect()
        second_uidvalidity = second_fetcher.select_folder(mailbox)
        assert second_uidvalidity != first_uidvalidity
        result = sync_account(
            second_fetcher,
            repository,
            storage,
            manifest,
            account_id=account_id,
            options=SyncOptions(),
            cancel=CancelToken(),
        )
        assert result.fetched_count == 1
    finally:
        second_fetcher.disconnect()
        manifest.close()

    generations = connection.execute(
        "SELECT uidvalidity, relative_path, file_hash FROM messages "
        "WHERE account_id = ? ORDER BY uidvalidity",
        (account_id,),
    ).fetchall()
    assert len(generations) == 2
    assert generations[0][0] != generations[1][0]
    assert generations[0][1] == generations[1][1]
    assert generations[0][2] == generations[1][2]
    old_failure = connection.execute(
        "SELECT uidvalidity, uid, error_class FROM sync_failures "
        "WHERE account_id = ? AND folder_id = ?",
        (account_id, folder_id),
    ).fetchone()
    assert old_failure == (first_uidvalidity, 1, "transient")
    cursor = connection.execute(
        "SELECT uidvalidity, last_seen_uid, backfill_next_uid FROM folders WHERE id = ?",
        (folder_id,),
    ).fetchone()
    assert cursor == (second_uidvalidity, 1, 0)
