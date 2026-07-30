from __future__ import annotations

import ssl
from pathlib import Path

import pytest

from mail_dock.domain.errors import TransientError
from mail_dock.domain.fetcher import CancelToken
from mail_dock.infrastructure.fetchers.onamae_imap import OnamaeImapFetcher
from mail_dock.infrastructure.storage.eml_storage import EmlStorage
from mail_dock.infrastructure.storage.manifest import ManifestWriter
from mail_dock.infrastructure.storage.storage_root import initialize_root
from mail_dock.usecases.sync_mail import SyncOptions, sync_account
from tests.support.imap_integration import (
    append_message,
    create_mailbox,
    imap_client,
    insecure_ssl_context,
    open_repository,
    register_account_and_folder,
    service,
    unique_mailbox,
)


class TransientFailureFetcher(OnamaeImapFetcher):
    """Inject connection-loss errors at the provider boundary for one UID."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        port: int,
        timeout: float,
        read_timeout: float,
        ssl_context: ssl.SSLContext,
        failures: int,
    ) -> None:
        super().__init__(
            host,
            username,
            password,
            port=port,
            timeout=timeout,
            read_timeout=read_timeout,
            ssl_context=ssl_context,
        )
        self.remaining_failures = failures

    def download_eml_bytes(self, raw_name: str, uid: int) -> bytes:
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise TransientError(f"injected connection loss for {raw_name}:{uid}")
        return super().download_eml_bytes(raw_name, uid)


@pytest.mark.docker
def test_transient_fetch_failure_is_recorded_then_retried_next_sync(tmp_path: Path) -> None:
    settings = service("dovecot")
    mailbox = unique_mailbox("FetchFailure")
    with imap_client(settings) as client:
        create_mailbox(client, mailbox)
        append_message(client, mailbox, body="retry after connection loss")

    account_id = "integration-fetch-failure"
    repository, connection = open_repository(tmp_path)
    root = tmp_path / "storage"
    initialize_root(root)
    folder_id = register_account_and_folder(repository, account_id, mailbox)
    storage = EmlStorage(root)
    manifest = ManifestWriter(root, account_id)
    fetcher = TransientFailureFetcher(
        settings.host,
        settings.username,
        settings.password,
        port=settings.port,
        timeout=5.0,
        read_timeout=5.0,
        ssl_context=insecure_ssl_context(),
        failures=4,
    )
    try:
        fetcher.connect()
        first = sync_account(
            fetcher,
            repository,
            storage,
            manifest,
            account_id=account_id,
            options=SyncOptions(),
            cancel=CancelToken(),
        )
        assert first.failed_count == 1
        failure = connection.execute(
            "SELECT attempt_count, error_class FROM sync_failures "
            "WHERE account_id = ? AND folder_id = ?",
            (account_id, folder_id),
        ).fetchone()
        assert failure == (1, "transient")

        second = sync_account(
            fetcher,
            repository,
            storage,
            manifest,
            account_id=account_id,
            options=SyncOptions(),
            cancel=CancelToken(),
        )
        assert second.fetched_count == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM sync_failures WHERE account_id = ? AND folder_id = ?",
            (account_id, folder_id),
        ).fetchone() == (0,)
    finally:
        fetcher.disconnect()
        manifest.close()
