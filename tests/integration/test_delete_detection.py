from __future__ import annotations

from pathlib import Path

import pytest

from mail_dock.domain.fetcher import CancelToken
from mail_dock.infrastructure.storage.eml_storage import EmlStorage
from mail_dock.infrastructure.storage.manifest import ManifestWriter
from mail_dock.infrastructure.storage.storage_root import initialize_root
from mail_dock.usecases.sync_mail import SyncOptions, sync_account
from tests.support.imap_integration import (
    append_message,
    append_raw_message,
    create_mailbox,
    imap_client,
    make_fetcher,
    open_repository,
    register_account_and_folder,
    service,
    unique_mailbox,
)


@pytest.mark.docker
def test_delete_detection_marks_moved_deleted_and_unknown_without_purging_eml(
    tmp_path: Path,
) -> None:
    settings = service("dovecot")
    moved_source = unique_mailbox("MovedSource")
    moved_target = unique_mailbox("MovedTarget")
    deleted_source = unique_mailbox("DeletedSource")
    unknown_source = unique_mailbox("UnknownSource")
    unknown_target_a = unique_mailbox("UnknownTargetA")
    unknown_target_b = unique_mailbox("UnknownTargetB")
    with imap_client(settings) as client:
        for mailbox in (
            moved_source,
            moved_target,
            deleted_source,
            unknown_source,
            unknown_target_a,
            unknown_target_b,
        ):
            create_mailbox(client, mailbox)
        moved_raw = append_message(client, moved_source, body="moved detection")
        append_raw_message(client, moved_target, moved_raw)
        append_message(client, deleted_source, body="deleted detection")
        unknown_raw = append_message(client, unknown_source, body="unknown detection")
        append_raw_message(client, unknown_target_a, unknown_raw)
        append_raw_message(client, unknown_target_b, unknown_raw)

    account_id = "integration-delete-detection"
    repository, connection = open_repository(tmp_path)
    root = tmp_path / "storage"
    initialize_root(root)
    folder_ids = {
        mailbox: register_account_and_folder(repository, account_id, mailbox)
        for mailbox in (
            moved_source,
            moved_target,
            deleted_source,
            unknown_source,
            unknown_target_a,
            unknown_target_b,
        )
    }
    storage = EmlStorage(root)
    manifest = ManifestWriter(root, account_id)
    fetcher = make_fetcher(settings)
    try:
        fetcher.connect()
        sync_account(
            fetcher,
            repository,
            storage,
            manifest,
            account_id=account_id,
            options=SyncOptions(),
            cancel=CancelToken(),
        )
        message_paths = {
            mailbox: connection.execute(
                "SELECT relative_path FROM messages WHERE folder_id = ?", (folder_ids[mailbox],)
            ).fetchone()[0]
            for mailbox in (moved_source, deleted_source, unknown_source)
        }
    finally:
        fetcher.disconnect()

    with imap_client(settings) as client:
        for mailbox in (moved_source, deleted_source, unknown_source):
            status, data = client.select(mailbox)
            assert status == "OK", data
            status, search = client.uid("SEARCH", "ALL")
            assert status == "OK", search
            uid = int(search[0].split()[-1])
            status, data = client.uid("STORE", str(uid), "+FLAGS.SILENT", r"(\Deleted)")
            assert status == "OK", data
            status, data = client.expunge()
            assert status == "OK", data

    fetcher = make_fetcher(settings)
    try:
        fetcher.connect()
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
        manifest.close()

    states = connection.execute(
        "SELECT folder_id, remote_state, moved_to_folder_id, relative_path "
        "FROM messages WHERE account_id = ? ORDER BY folder_id",
        (account_id,),
    ).fetchall()
    by_folder = {row[0]: row[1:] for row in states}
    assert by_folder[folder_ids[moved_source]][0] == "moved"
    assert by_folder[folder_ids[moved_source]][1] == folder_ids[moved_target]
    assert by_folder[folder_ids[deleted_source]][0] == "deleted"
    assert by_folder[folder_ids[deleted_source]][1] is None
    assert by_folder[folder_ids[unknown_source]][0] == "unknown"
    assert by_folder[folder_ids[unknown_source]][1] is None
    for relative_path in message_paths.values():
        assert relative_path is not None
        assert (root / relative_path).is_file()
