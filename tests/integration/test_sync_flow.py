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
    assert len(events) == 3
    assert events[0]["event"] == "folder_snapshot"
    assert events[1]["event"] == "fetch"
    assert events[1]["relative_path"] == relative_path
    assert events[1]["file_hash"] == file_hash
    assert events[2]["event"] == "checkpoint"


@pytest.mark.docker
def test_flag_refresh_applies_server_changes_and_advances_modseq_after_success(
    tmp_path: Path,
) -> None:
    settings = service("dovecot")
    mailbox = unique_mailbox("FlagRefresh")
    with imap_client(settings) as client:
        create_mailbox(client, mailbox)
        append_message(client, mailbox, body="flag refresh integration body")

    account_id = "integration-flag-refresh"
    repository, connection = open_repository(tmp_path)
    root = tmp_path / "storage"
    initialize_root(root)
    register_account_and_folder(repository, account_id, mailbox)
    storage = EmlStorage(root)
    manifest = ManifestWriter(root, account_id)
    fetcher = make_fetcher(settings)
    options = SyncOptions(flag_refresh_min_interval_seconds=3600)
    old_seen_at = "2026-01-01T00:00:00Z"
    try:
        fetcher.connect()
        first = sync_account(
            fetcher,
            repository,
            storage,
            manifest,
            account_id=account_id,
            options=options,
            cancel=CancelToken(),
        )
        assert first.fetched_count == 1

        connection.execute(
            "UPDATE messages SET flags_seen_at = ? WHERE account_id = ?",
            (old_seen_at, account_id),
        )
        baseline = sync_account(
            fetcher,
            repository,
            storage,
            manifest,
            account_id=account_id,
            options=options,
            cancel=CancelToken(),
        )
        assert baseline.cancelled is False
        baseline_modseq = repository.list_folders(account_id)[0].get("highest_modseq")
        assert "CONDSTORE" in fetcher.capabilities
        assert isinstance(baseline_modseq, int) and baseline_modseq > 0

        row = connection.execute(
            "SELECT uid FROM messages WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        assert row is not None
        uid = int(row[0])
        with imap_client(settings) as client:
            status, data = client.select(mailbox)
            assert status == "OK", data
            status, data = client.uid("STORE", str(uid), "+FLAGS.SILENT", r"(\Seen)")
            assert status == "OK", data

        connection.execute(
            "UPDATE messages SET flags_seen_at = ? WHERE account_id = ?",
            (old_seen_at, account_id),
        )
        refreshed = sync_account(
            fetcher,
            repository,
            storage,
            manifest,
            account_id=account_id,
            options=options,
            cancel=CancelToken(),
        )
    finally:
        fetcher.disconnect()
        manifest.close()

    assert refreshed.cancelled is False
    message = connection.execute(
        "SELECT imap_flags, flags_seen_at FROM messages WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    assert message is not None
    assert r"\Seen" in str(message[0]).split()
    assert message[1] != old_seen_at
    assert repository.list_folders(account_id)[0]["highest_modseq"] > baseline_modseq
