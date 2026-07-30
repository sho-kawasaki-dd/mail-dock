from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from mail_dock.domain.fetcher import CancelToken
from mail_dock.infrastructure.storage.eml_storage import EmlStorage
from mail_dock.infrastructure.storage.manifest import ManifestWriter, read_events
from mail_dock.infrastructure.storage.storage_root import initialize_root
from mail_dock.usecases.sync_mail import SyncOptions, sync_account
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
def test_real_sync_keeps_eml_manifest_and_database_consistent(tmp_path: Path) -> None:
    settings = service("dovecot")
    mailbox = unique_mailbox("SyncFlow")
    with imap_client(settings) as client:
        create_mailbox(client, mailbox)
        append_message(client, mailbox, body="end-to-end sync body")

    account_id = "integration-sync"
    repository, connection = open_repository(tmp_path)
    root = tmp_path / "storage"
    initialize_root(root)
    register_account_and_folder(repository, account_id, mailbox)
    storage = EmlStorage(root)
    manifest = ManifestWriter(root, account_id)
    fetcher = make_fetcher(settings)
    try:
        fetcher.connect()
        result = sync_account(
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

    assert result.fetched_count == 1
    message = connection.execute(
        "SELECT relative_path, file_hash, size_bytes FROM messages WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    assert message is not None
    relative_path, file_hash, size_bytes = message
    eml_path = root / str(relative_path)
    raw = eml_path.read_bytes()
    assert eml_path.is_file()
    assert hashlib.sha256(raw).hexdigest() == file_hash
    assert len(raw) == size_bytes

    manifest_paths = list((root / "manifests" / "imap" / account_id).glob("events-*.jsonl"))
    assert len(manifest_paths) == 1
    events = list(read_events(manifest_paths[0]))
    assert len(events) == 1
    assert events[0]["event"] == "fetch"
    assert events[0]["relative_path"] == relative_path
    assert events[0]["file_hash"] == file_hash
