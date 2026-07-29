"""Smoke tests for the Docker-provided GreenMail IMAP service."""

from __future__ import annotations

import imaplib
import os
import ssl
import time

import pytest


@pytest.mark.docker
def test_greenmail_imaps_login_and_list() -> None:
    """Connect to GreenMail over IMAPS, authenticate, and list mailboxes."""

    host = os.environ.get("MAILDOCK_GREENMAIL_HOST", "127.0.0.1")
    port = int(os.environ.get("MAILDOCK_GREENMAIL_IMAPS_PORT", "3993"))
    deadline = time.monotonic() + 30.0
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        try:
            with imaplib.IMAP4_SSL(
                host,
                port,
                ssl_context=_greenmail_ssl_context(),
                timeout=2.0,
            ) as client:
                login_status, _ = client.login("testuser", "password")
                assert login_status == "OK"

                list_status, _ = client.list()
                assert list_status == "OK"
                return
        except (OSError, imaplib.IMAP4.error) as error:
            last_error = error
            time.sleep(0.5)

    pytest.fail(f"GreenMail did not accept IMAPS connections within 30 seconds: {last_error!r}")


def _greenmail_ssl_context() -> ssl.SSLContext:
    """Return a test-only context for GreenMail's self-signed certificate."""

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context
