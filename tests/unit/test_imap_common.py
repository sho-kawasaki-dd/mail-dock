import imaplib
import socket
import ssl
from datetime import UTC

import pytest

from mail_dock.domain.errors import (
    AuthenticationError,
    PermanentError,
    StorageDetachedError,
    TransientError,
)
from mail_dock.infrastructure.fetchers.imap_common import (
    decode_modified_utf7,
    encode_modified_utf7,
    parse_fetch_response,
    parse_internaldate,
    parse_list_response,
    wrap_imap_errors,
)


@pytest.mark.parametrize("value", ["Inbox", "受信トレイ/請求書", "A&B"])
def test_modified_utf7_round_trip(value: str) -> None:
    assert decode_modified_utf7(encode_modified_utf7(value)) == value


def test_list_response_decodes_folder_and_special_use() -> None:
    response = b'* LIST (\\HasNoChildren \\Trash) "." "&U9c-"'
    folder = parse_list_response(response)

    assert folder.raw_name == "&U9c-"
    assert folder.display_name == "受"
    assert folder.delimiter == "."
    assert folder.special_use == frozenset({r"\HasNoChildren", r"\Trash"})


def test_lsub_response_uses_the_same_folder_parser() -> None:
    folder = parse_list_response(b'* LSUB () "/" "INBOX"')

    assert folder.raw_name == "INBOX"
    assert folder.display_name == "INBOX"


def test_fetch_response_parses_metadata_and_message_id() -> None:
    response = (
        b'* 7 FETCH (UID 42 INTERNALDATE "30-Jul-2026 12:34:56 +0900" '
        b"RFC822.SIZE 123 FLAGS (\\Seen \\Flagged) BODY[HEADER] {35}",
        b"Message-ID: <m@example.test>\r\nSubject: test\r\n",
    )

    result = parse_fetch_response(response)

    assert result.uid == 42
    assert result.size_bytes == 123
    assert result.message_id == "<m@example.test>"
    assert result.flags == (r"\Seen", r"\Flagged")
    assert result.internal_date is not None
    assert result.internal_date.isoformat() == "2026-07-30T03:34:56+00:00"


def test_fetch_response_parses_single_bytes_without_literal() -> None:
    result = parse_fetch_response(b"* 7 FETCH (UID 42 FLAGS (\\Seen \\Flagged))")

    assert result.uid == 42
    assert result.flags == (r"\Seen", r"\Flagged")
    assert result.message_id is None


def test_internaldate_is_utc() -> None:
    assert parse_internaldate("30-Jul-2026 12:34:56 +0900").tzinfo == UTC


def test_imap_errors_are_translated() -> None:
    assert socket.timeout is TimeoutError
    with pytest.raises(AuthenticationError), wrap_imap_errors("LOGIN"):
        raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] invalid credentials")
    with pytest.raises(TransientError), wrap_imap_errors("FETCH"):
        raise TimeoutError("timed out")
    with pytest.raises(TransientError), wrap_imap_errors("FETCH"):
        raise imaplib.IMAP4.abort("connection closed")
    with pytest.raises(TransientError), wrap_imap_errors("FETCH"):
        raise ConnectionError("connection reset")
    with pytest.raises(PermanentError), wrap_imap_errors("LIST"):
        raise ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED")


def test_detached_os_error_keeps_storage_classification() -> None:
    error = OSError(5, "detached")
    error.errno = 5

    with pytest.raises(StorageDetachedError), wrap_imap_errors("FETCH"):
        raise error
