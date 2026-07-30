"""Helpers for integration tests against the Compose IMAP services."""

from __future__ import annotations

import imaplib
import os
import sqlite3
import ssl
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

import pytest

from mail_dock.infrastructure.database.connection import connect
from mail_dock.infrastructure.database.message_repository import SqliteMessageRepository
from mail_dock.infrastructure.database.migrator import migrate
from mail_dock.infrastructure.fetchers.onamae_imap import OnamaeImapFetcher


@dataclass(frozen=True)
class ImapService:
    """Connection settings for one Docker-provided IMAP service."""

    name: str
    host: str
    port: int
    username: str
    password: str


def service(name: str) -> ImapService:
    """Build service settings from the documented test environment variables."""

    normalized = name.upper()
    defaults = {
        "GREENMAIL": ("127.0.0.1", 3993),
        "DOVECOT": ("127.0.0.1", 3994),
    }
    try:
        default_host, default_port = defaults[normalized]
    except KeyError as error:
        raise ValueError(f"unknown IMAP service: {name}") from error
    return ImapService(
        name=normalized.lower(),
        host=os.environ.get(f"MAILDOCK_{normalized}_HOST", default_host),
        port=int(os.environ.get(f"MAILDOCK_{normalized}_IMAPS_PORT", str(default_port))),
        username=os.environ.get(f"MAILDOCK_{normalized}_USERNAME", "testuser"),
        password=os.environ.get(f"MAILDOCK_{normalized}_PASSWORD", "password"),
    )


def insecure_ssl_context() -> ssl.SSLContext:
    """Return the test-only context for Compose's generated certificates."""

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def make_fetcher(settings: ImapService) -> OnamaeImapFetcher:
    """Construct the production fetcher for a Compose service."""

    return OnamaeImapFetcher(
        settings.host,
        settings.username,
        settings.password,
        port=settings.port,
        timeout=5.0,
        read_timeout=5.0,
        ssl_context=insecure_ssl_context(),
    )


@contextmanager
def imap_client(settings: ImapService) -> Iterator[imaplib.IMAP4_SSL]:
    """Open a direct test connection, waiting briefly for Docker health."""

    deadline = time.monotonic() + 30.0
    last_error: Exception | None = None
    client: imaplib.IMAP4_SSL | None = None
    while time.monotonic() < deadline:
        try:
            client = imaplib.IMAP4_SSL(
                settings.host,
                settings.port,
                ssl_context=insecure_ssl_context(),
                timeout=2.0,
            )
            status, _ = client.login(settings.username, settings.password)
            if status != "OK":
                raise RuntimeError(f"IMAP LOGIN failed: {status}")
        except (OSError, imaplib.IMAP4.error, RuntimeError) as error:
            last_error = error
            if client is not None:
                with suppress(OSError, imaplib.IMAP4.error):
                    client.logout()
            time.sleep(0.5)
            client = None
        else:
            break
    if client is None:
        pytest.fail(f"{settings.name} did not accept IMAPS connections: {last_error!r}")
    try:
        yield client
    finally:
        with suppress(OSError, imaplib.IMAP4.error):
            client.logout()


def unique_mailbox(prefix: str = "MailDock") -> str:
    """Return an isolated ASCII mailbox name for one test scenario."""

    return f"{prefix}-{uuid.uuid4().hex}"


def create_mailbox(client: imaplib.IMAP4_SSL, mailbox: str) -> None:
    """Create a mailbox and tolerate an already-created fixture mailbox."""

    status, data = client.create(mailbox)
    error_text = b" ".join(
        item if isinstance(item, bytes) else str(item).encode() for item in data or []
    ).upper()
    if (
        status != "OK"
        and b"ALREADYEXISTS" not in error_text
        and b"ALREADY EXISTS" not in error_text
    ):
        raise RuntimeError(f"could not create mailbox {mailbox!r}: {data!r}")


def append_message(
    client: imaplib.IMAP4_SSL,
    mailbox: str,
    *,
    message_id: str | None = None,
    subject: str = "mail-dock integration",
    body: str = "integration body",
) -> bytes:
    """Append a small RFC-compliant message and return its exact bytes."""

    message = EmailMessage()
    message["From"] = "sender@example.test"
    message["To"] = "recipient@example.test"
    message["Subject"] = subject
    message["Message-ID"] = message_id or f"<{uuid.uuid4().hex}@example.test>"
    message["Date"] = "Thu, 30 Jul 2026 12:00:00 +0000"
    message.set_content(body)
    raw = message.as_bytes()
    status, data = client.append(mailbox, None, datetime.now(UTC), raw)
    if status != "OK":
        raise RuntimeError(f"could not append message to {mailbox!r}: {data!r}")
    return raw


def append_raw_message(client: imaplib.IMAP4_SSL, mailbox: str, raw: bytes) -> None:
    """Append already-built bytes without changing their content."""

    status, data = client.append(mailbox, None, datetime.now(UTC), raw)
    if status != "OK":
        raise RuntimeError(f"could not append raw message to {mailbox!r}: {data!r}")


def expunge_uid(client: imaplib.IMAP4_SSL, mailbox: str, uid: int) -> None:
    """Expunge one UID from an isolated test mailbox."""

    status, data = client.select(mailbox)
    if status != "OK":
        raise RuntimeError(f"could not select {mailbox!r}: {data!r}")
    status, data = client.uid("STORE", str(uid), "+FLAGS.SILENT", r"(\Deleted)")
    if status != "OK":
        raise RuntimeError(f"could not mark UID {uid} deleted: {data!r}")
    status, data = client.expunge()
    if status != "OK":
        raise RuntimeError(f"could not expunge UID {uid}: {data!r}")


def open_repository(tmp_path: Path) -> tuple[SqliteMessageRepository, sqlite3.Connection]:
    """Create and migrate a real SQLite repository for one integration test."""

    connection = connect(tmp_path / "metadata.db")
    migrate(connection, tmp_path / "metadata.db")
    return SqliteMessageRepository(connection), connection


def register_account_and_folder(
    repository: SqliteMessageRepository,
    account_id: str,
    mailbox: str,
    *,
    enabled: bool = True,
) -> int:
    """Register one account and one explicitly selected sync folder."""

    repository.upsert_account(
        {
            "id": account_id,
            "provider_type": "imap",
            "host": "integration.test",
            "port": 993,
            "username": "testuser",
        }
    )
    folder_id = repository.upsert_folder(
        {
            "account_id": account_id,
            "raw_name": mailbox,
            "display_name": mailbox,
            "is_sync_target": int(enabled),
        }
    )
    return int(folder_id)
