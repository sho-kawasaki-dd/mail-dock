"""Docker/Dovecot integration tests for the server-side deletion safety net.

These exercise the real provider (`OnamaeImapFetcher`) against a live IMAP
server for behavior that a Fake fetcher cannot prove: SPECIAL-USE Trash
discovery, MOVE/EXPUNGE semantics, and reconciling an uncertain delete
against the server's actual state (see review notes 3.4 / 9.3).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from mail_dock.domain.ports import BaseManifestReader, JSONValue
from mail_dock.domain.storage_state import StorageState, StorageStateMachine
from mail_dock.infrastructure.storage.manifest import ManifestReader, ManifestWriter
from mail_dock.usecases.delete_remote import reconcile_uncertain_deletes
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


class _CombinedManifest(BaseManifestReader):
    """A `BaseManifestReader` that also exposes the writer used to seed events.

    `reconcile_uncertain_deletes()` only requires read access plus an
    ``append``/``flush_and_sync`` writer, which it discovers via ``.writer``.
    """

    def __init__(self, root: Path, account_id: str) -> None:
        self.writer = ManifestWriter(root, account_id)
        self._reader = ManifestReader(root, account_id)

    def read_all_events(self) -> Iterator[Mapping[str, JSONValue]]:
        return self._reader.read_all_events()

    def read_last_checkpoint(self) -> Mapping[str, JSONValue] | None:
        return self._reader.read_last_checkpoint()

    def read_events_since_checkpoint(self) -> Iterator[Mapping[str, JSONValue]]:
        return self._reader.read_events_since_checkpoint()

    def read_incomplete_intents(self) -> Iterator[Mapping[str, JSONValue]]:
        return self._reader.read_incomplete_intents()

    def close(self) -> None:
        self.writer.close()


def _add_message(
    repository: Any,
    *,
    account_id: str,
    folder_id: int,
    uid: int,
    uidvalidity: int,
) -> int:
    return int(
        repository.add_message(
            {
                "account_id": account_id,
                "folder_id": folder_id,
                "uid": uid,
                "uidvalidity": uidvalidity,
                "source_item_key": f"{uidvalidity}:{uid}",
                "content_key": f"reconcile:{account_id}:{uidvalidity}:{uid}",
                "remote_state": "present",
                # A remote-delete candidate always has a verified local EML on disk
                # (D-12), so both list_stored_messages()'s `relative_path IS NOT NULL`
                # filter and _candidate_for_reconciliation()'s size_bytes/file_hash
                # checks must be satisfied here too.
                "relative_path": f"eml/reconcile-{uidvalidity}-{uid}.eml",
                "file_hash": f"{uidvalidity:064x}"[:64],
                "size_bytes": 42,
            }
        )
    )


@pytest.mark.docker
def test_find_trash_folder_uses_special_use() -> None:
    settings = service("dovecot")
    fetcher = make_fetcher(settings)
    fetcher.connect()
    try:
        trash = fetcher.find_trash_folder()
    finally:
        fetcher.disconnect()

    assert trash is not None
    assert trash.raw_name == "Trash"
    assert any(attribute.casefold() == r"\trash" for attribute in trash.special_use)


@pytest.mark.docker
def test_delete_remote_message_trash_mode_moves_to_special_use_trash() -> None:
    settings = service("dovecot")
    source = unique_mailbox("DeleteTrashSource")
    with imap_client(settings) as client:
        create_mailbox(client, source)
        append_message(client, source, subject="trash-move", body="trash move body")

    fetcher = make_fetcher(settings)
    fetcher.connect()
    try:
        fetcher.select_folder(source)
        uid = next(iter(fetcher.list_existing_uids(source)))
        trash = fetcher.find_trash_folder()
        assert trash is not None
        before_trash_uids = fetcher.list_existing_uids(trash.raw_name)

        fetcher.delete_remote_message(source, uid, mode="trash")

        assert uid not in fetcher.list_existing_uids(source)
        after_trash_uids = fetcher.list_existing_uids(trash.raw_name)
        assert len(after_trash_uids) == len(before_trash_uids) + 1
    finally:
        fetcher.disconnect()


@pytest.mark.docker
def test_delete_remote_message_expunge_mode_permanently_removes_without_moving() -> None:
    settings = service("dovecot")
    source = unique_mailbox("DeleteExpungeSource")
    with imap_client(settings) as client:
        create_mailbox(client, source)
        append_message(client, source, subject="expunge", body="expunge body")

    fetcher = make_fetcher(settings)
    fetcher.connect()
    try:
        if not fetcher.supports_uid_expunge():
            pytest.skip("this Dovecot instance does not advertise UIDPLUS")
        fetcher.select_folder(source)
        uid = next(iter(fetcher.list_existing_uids(source)))
        trash = fetcher.find_trash_folder()
        assert trash is not None
        before_trash_uids = fetcher.list_existing_uids(trash.raw_name)

        fetcher.delete_remote_message(source, uid, mode="expunge")

        assert uid not in fetcher.list_existing_uids(source)
        assert fetcher.list_existing_uids(trash.raw_name) == before_trash_uids
    finally:
        fetcher.disconnect()


@pytest.mark.docker
def test_reconcile_uncertain_delete_confirms_completion_against_the_real_server(
    tmp_path: Path,
) -> None:
    """``remote_delete_uncertain`` must resolve only by asking the server the truth."""
    settings = service("dovecot")
    deleted_mailbox = unique_mailbox("ReconcileDeleted")
    present_mailbox = unique_mailbox("ReconcilePresent")
    with imap_client(settings) as client:
        create_mailbox(client, deleted_mailbox)
        create_mailbox(client, present_mailbox)
        append_message(client, deleted_mailbox, subject="reconcile-deleted")
        append_message(client, present_mailbox, subject="reconcile-present")

    account_id = "integration-reconcile-uncertain"
    repository, _connection = open_repository(tmp_path)
    root = tmp_path / "storage"
    (root / "manifests" / "imap" / account_id).mkdir(parents=True)

    probe = make_fetcher(settings)
    probe.connect()
    try:
        deleted_uidvalidity = probe.select_folder(deleted_mailbox)
        deleted_uid = next(iter(probe.list_existing_uids(deleted_mailbox)))
        present_uidvalidity = probe.select_folder(present_mailbox)
        present_uid = next(iter(probe.list_existing_uids(present_mailbox)))
    finally:
        probe.disconnect()

    deleted_folder_id = register_account_and_folder(repository, account_id, deleted_mailbox)
    present_folder_id = register_account_and_folder(repository, account_id, present_mailbox)
    deleted_message_id = _add_message(
        repository,
        account_id=account_id,
        folder_id=deleted_folder_id,
        uid=deleted_uid,
        uidvalidity=deleted_uidvalidity,
    )
    present_message_id = _add_message(
        repository,
        account_id=account_id,
        folder_id=present_folder_id,
        uid=present_uid,
        uidvalidity=present_uidvalidity,
    )

    manifest = _CombinedManifest(root, account_id)
    try:
        for mailbox, uid, uidvalidity in (
            (deleted_mailbox, deleted_uid, deleted_uidvalidity),
            (present_mailbox, present_uid, present_uidvalidity),
        ):
            manifest.writer.append(
                {
                    "event": "remote_delete_intent",
                    "account_id": account_id,
                    "folder_raw_name": mailbox,
                    "uid": uid,
                    "uidvalidity": uidvalidity,
                    "mode": "trash",
                    "timestamp": "2026-08-27T00:00:00+00:00",
                }
            )
        manifest.writer.flush_and_sync()

        # A separate raw connection completes only the first delete on the server,
        # simulating a transient client-side error that hid a real success.
        with imap_client(settings) as raw_client:
            status, data = raw_client.select(deleted_mailbox)
            assert status == "OK", data
            status, data = raw_client.uid("STORE", str(deleted_uid), "+FLAGS.SILENT", r"(\Deleted)")
            assert status == "OK", data
            status, data = raw_client.expunge()
            assert status == "OK", data

        state = StorageStateMachine(StorageState.ATTACHED)
        fetcher = make_fetcher(settings)
        fetcher.connect()
        try:
            reconcile_uncertain_deletes(fetcher, repository, manifest, storage_state=state)
        finally:
            fetcher.disconnect()

        events = list(manifest.read_all_events())
    finally:
        manifest.close()

    completed_keys = {
        (event.get("folder_raw_name"), event.get("uid"))
        for event in events
        if event.get("event") == "remote_delete_completed"
    }
    assert (deleted_mailbox, deleted_uid) in completed_keys
    assert (present_mailbox, present_uid) not in completed_keys

    deleted_record = repository.get_message(deleted_message_id)
    present_record = repository.get_message(present_message_id)
    assert deleted_record is not None and deleted_record["remote_state"] == "deleted"
    assert present_record is not None and present_record["remote_state"] == "present"
