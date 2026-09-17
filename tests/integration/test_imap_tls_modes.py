"""Docker/Dovecot integration tests for Phase 5.1 TLS/authentication modes.

These exercise real TLS negotiation, real certificate verification, and real
SASL wire exchanges against Dovecot -- interactions that the FakeImap-based
unit tests in ``tests/unit/test_generic_imap.py`` cannot guarantee.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mail_dock.domain.errors import ConfigError, MailDockError
from mail_dock.infrastructure.fetchers.generic_imap import GenericImapFetcher
from tests.support.imap_integration import (
    append_message,
    create_mailbox,
    imap_client,
    make_fetcher,
    service,
    unique_mailbox,
)


@pytest.mark.docker
def test_starttls_connects_lists_folders_and_logs_in() -> None:
    dovecot = service("dovecot")
    mailbox = unique_mailbox("Starttls")
    with imap_client(dovecot) as client:
        create_mailbox(client, mailbox)
        append_message(client, mailbox, body="via starttls")

    fetcher = make_fetcher(service("dovecot_starttls"))
    try:
        fetcher.connect()
        assert "LOGINDISABLED" not in fetcher.capabilities
        folders = {folder.raw_name for folder in fetcher.list_folders()}
        assert mailbox in folders

        uidvalidity = fetcher.select_folder(mailbox)
        assert uidvalidity > 0
        refs = list(fetcher.iter_message_refs(mailbox))
        assert len(refs) == 1
    finally:
        fetcher.disconnect()


@pytest.mark.docker
def test_implicit_tls_is_rejected_when_starttls_is_required() -> None:
    starttls = service("dovecot_starttls")
    fetcher = GenericImapFetcher(
        starttls.host,
        starttls.username,
        starttls.password,
        port=starttls.port,
        timeout=5.0,
        read_timeout=5.0,
        tls_mode="implicit",
    )

    with pytest.raises(MailDockError):
        fetcher.connect()


@pytest.mark.docker
def test_logindisabled_server_falls_back_to_sasl_plain() -> None:
    fetcher = make_fetcher(service("dovecot_logindisabled"))
    try:
        fetcher.connect()
        assert "LOGINDISABLED" in fetcher.capabilities
        folders = {folder.raw_name for folder in fetcher.list_folders()}
        assert "INBOX" in folders
    finally:
        fetcher.disconnect()


@pytest.mark.docker
def test_custom_ca_certificate_verifies_successfully() -> None:
    settings = service("dovecot_ca")
    assert settings.ca_cert_path is not None
    assert Path(settings.ca_cert_path).is_file(), (
        "expected the dovecot-ca container to export its CA certificate to "
        f"{settings.ca_cert_path}; is the compose stack running?"
    )

    fetcher = make_fetcher(settings)
    try:
        fetcher.connect()
        folders = {folder.raw_name for folder in fetcher.list_folders()}
        assert "INBOX" in folders
    finally:
        fetcher.disconnect()


@pytest.mark.docker
def test_wrong_ca_certificate_fails_verification(tmp_path: Path) -> None:
    settings = service("dovecot_ca")
    wrong_ca = tmp_path / "wrong-ca.pem"
    wrong_key = tmp_path / "wrong-ca.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-nodes",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(wrong_key),
            "-out",
            str(wrong_ca),
            "-days",
            "1",
            "-subj",
            "/CN=not-the-real-ca",
        ],
        check=True,
        capture_output=True,
    )

    fetcher = GenericImapFetcher(
        settings.host,
        settings.username,
        settings.password,
        port=settings.port,
        timeout=5.0,
        read_timeout=5.0,
        ca_cert_path=str(wrong_ca),
    )

    with pytest.raises(MailDockError):
        fetcher.connect()


@pytest.mark.docker
def test_missing_ca_certificate_file_is_a_config_error() -> None:
    settings = service("dovecot_ca")
    fetcher = GenericImapFetcher(
        settings.host,
        settings.username,
        settings.password,
        port=settings.port,
        timeout=5.0,
        read_timeout=5.0,
        ca_cert_path="/nonexistent/mail-dock-ca.pem",
    )

    with pytest.raises(ConfigError):
        fetcher.connect()
