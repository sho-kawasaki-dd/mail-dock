from __future__ import annotations

from pathlib import Path

import pytest

from mail_dock.domain.fetcher import CancelToken
from mail_dock.infrastructure.database.message_folder_migration import finalize_message_folders
from mail_dock.infrastructure.storage.eml_storage import EmlStorage
from mail_dock.infrastructure.storage.manifest import ManifestReader, ManifestWriter
from mail_dock.infrastructure.storage.storage_root import initialize_root
from mail_dock.usecases.sync_mail import SyncOptions
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
from tests.support.usecase_adapters import sync_account


@pytest.mark.docker
def test_delete_detection_marks_moved_deleted_and_unknown_without_purging_eml(
    tmp_path: Path,
) -> None:
    """A MOVE is only merged when the destination is a fresh, same-cycle fetch.

    ``moved_target`` deliberately receives its copy of the message *between*
    the two syncs (not before the first one), so the second sync observes it
    as newly discovered in the same cycle the source UID disappears in —
    exactly the evidence required to merge instead of leaving duplicates.
    """

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

    def writer_factory(target_account_id: str) -> ManifestWriter:
        return ManifestWriter(root, target_account_id)

    def reader_factory(target_account_id: str) -> ManifestReader:
        return ManifestReader(root, target_account_id)

    finalize_message_folders(connection, writer_factory, reader_factory)

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
                "SELECT m.relative_path FROM messages AS m "
                "JOIN message_folders AS mf ON mf.message_id = m.id "
                "WHERE m.account_id = ? AND mf.folder_id = ?",
                (account_id, folder_ids[mailbox]),
            ).fetchone()[0]
            for mailbox in (moved_source, deleted_source, unknown_source)
        }
    finally:
        fetcher.disconnect()

    with imap_client(settings) as client:
        # The MOVE destination only appears now, in the same cycle the
        # source disappears in, so it counts as fresh evidence of a move.
        append_raw_message(client, moved_target, moved_raw)
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

    def membership(folder_id: int) -> tuple[str, int | None]:
        row = connection.execute(
            "SELECT mf.remote_state, mf.moved_to_folder_id FROM message_folders AS mf "
            "JOIN messages AS m ON m.id = mf.message_id "
            "WHERE m.account_id = ? AND mf.folder_id = ?",
            (account_id, folder_id),
        ).fetchone()
        assert row is not None
        return row[0], row[1]

    assert membership(folder_ids[moved_source]) == ("moved", folder_ids[moved_target])
    assert membership(folder_ids[moved_target])[0] == "present"
    assert membership(folder_ids[deleted_source]) == ("deleted", None)
    assert membership(folder_ids[unknown_source]) == ("unknown", None)

    # The MOVE merges into one canonical row rather than leaving a duplicate.
    duplicate_count = connection.execute(
        "SELECT COUNT(*) FROM messages WHERE account_id = ? AND relative_path = ?",
        (account_id, message_paths[moved_source]),
    ).fetchone()[0]
    assert duplicate_count == 1

    for relative_path in message_paths.values():
        assert relative_path is not None
        assert (root / relative_path).is_file()
