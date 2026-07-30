"""Provider-independent IMAP response parsing and exception translation.

The functions in this module keep ``imaplib`` response details at the
infrastructure boundary. Fetcher implementations must call remote operations
inside :func:`wrap_imap_errors`; retry and backoff remain use-case concerns.
"""

from __future__ import annotations

import base64
import binascii
import imaplib
import re
import ssl
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime

from mail_dock.domain.errors import (
    AuthenticationError,
    MailDockError,
    PermanentError,
    TransientError,
)
from mail_dock.domain.fetcher import RemoteFolder, RemoteMessageRef
from mail_dock.infrastructure.storage.detach import classify_os_error

_LIST_PREFIX = re.compile(r"^\s*\*\s+(?:LIST|LSUB)\s+", re.IGNORECASE)
_UID_PATTERN = re.compile(r"\bUID\s+(\d+)", re.IGNORECASE)
_SIZE_PATTERN = re.compile(r"\bRFC822\.SIZE\s+(\d+)", re.IGNORECASE)
_DATE_PATTERN = re.compile(r'\bINTERNALDATE\s+["\']([^"\']+)["\']', re.IGNORECASE)
_FLAGS_PATTERN = re.compile(r"\bFLAGS\s*\(([^)]*)\)", re.IGNORECASE)

type ImapResponse = bytes | str
type FetchResponse = bytes | tuple[bytes, bytes] | list[object]


def encode_modified_utf7(value: str) -> str:
    """Encode a mailbox name using IMAP's modified UTF-7 representation."""

    encoded: list[str] = []
    non_ascii: list[str] = []

    def flush_non_ascii() -> None:
        if not non_ascii:
            return
        raw = "".join(non_ascii).encode("utf-16-be")
        token = base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",")
        encoded.append(f"&{token}-")
        non_ascii.clear()

    for character in value:
        if 0x20 <= ord(character) <= 0x7E and character != "&":
            flush_non_ascii()
            encoded.append(character)
        elif character == "&":
            flush_non_ascii()
            encoded.append("&-")
        else:
            non_ascii.append(character)
    flush_non_ascii()
    return "".join(encoded)


def decode_modified_utf7(value: str) -> str:
    """Decode an IMAP modified UTF-7 mailbox name."""

    decoded: list[str] = []
    position = 0
    while position < len(value):
        ampersand = value.find("&", position)
        if ampersand < 0:
            decoded.append(value[position:])
            break
        decoded.append(value[position:ampersand])
        end = value.find("-", ampersand)
        if end < 0:
            raise ValueError("unterminated modified UTF-7 shift sequence")
        token = value[ampersand + 1 : end]
        if not token:
            decoded.append("&")
        else:
            try:
                base64_token = token.replace(",", "/")
                padding = "=" * (-len(base64_token) % 4)
                raw = base64.b64decode(base64_token + padding, validate=True)
                decoded.append(raw.decode("utf-16-be"))
            except (ValueError, UnicodeDecodeError, binascii.Error) as error:
                raise ValueError("invalid modified UTF-7 shift sequence") from error
        position = end + 1
    return "".join(decoded)


def parse_list_response(response: ImapResponse) -> RemoteFolder:
    """Parse one untagged IMAP ``LIST`` response into a domain folder."""

    text = _response_text(response)
    match = _LIST_PREFIX.match(text)
    if match is None and not text.lstrip().startswith("("):
        raise PermanentError(f"invalid IMAP LIST response: {text[:80]}")

    position = match.end() if match is not None else text.index("(")
    if position >= len(text) or text[position] != "(":
        raise PermanentError("IMAP LIST response has no attribute list")
    attributes_end = text.find(")", position + 1)
    if attributes_end < 0:
        raise PermanentError("IMAP LIST response has an unterminated attribute list")
    attributes = tuple(text[position + 1 : attributes_end].split())
    delimiter, position = _read_imap_token(text, attributes_end + 1)
    raw_name, position = _read_imap_token(text, position)
    if not raw_name:
        raise PermanentError("IMAP LIST response has an empty mailbox name")
    try:
        display_name = decode_modified_utf7(raw_name)
    except ValueError as error:
        raise PermanentError("IMAP LIST response has an invalid mailbox name") from error
    return RemoteFolder(
        raw_name=raw_name,
        display_name=display_name,
        special_use=frozenset(attributes),
        delimiter=None if delimiter.upper() == "NIL" else delimiter,
    )


def parse_list_responses(responses: Iterable[ImapResponse]) -> list[RemoteFolder]:
    """Parse all untagged ``LIST`` responses returned by ``imaplib``."""

    return [parse_list_response(response) for response in responses]


def parse_internaldate(value: str) -> datetime:
    """Parse an IMAP ``INTERNALDATE`` value and normalize it to UTC."""

    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise PermanentError(f"invalid IMAP INTERNALDATE: {value!r}") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_internal_date(value: str) -> datetime:
    """Compatibility spelling for :func:`parse_internaldate`."""

    return parse_internaldate(value)


def parse_fetch_response(response: FetchResponse) -> RemoteMessageRef:
    """Parse one ``UID FETCH`` response into a provider-neutral message ref."""

    metadata, literal = _fetch_parts(response)
    metadata_text = metadata.decode("ascii", errors="replace")
    uid_match = _UID_PATTERN.search(metadata_text)
    if uid_match is None:
        raise PermanentError("IMAP FETCH response has no UID")
    date_match = _DATE_PATTERN.search(metadata_text)
    internal_date = parse_internaldate(date_match.group(1)) if date_match else None
    size_match = _SIZE_PATTERN.search(metadata_text)
    flags_match = _FLAGS_PATTERN.search(metadata_text)
    flags = tuple(flags_match.group(1).split()) if flags_match else ()
    message_id = _message_id_from_headers(literal)
    return RemoteMessageRef(
        uid=int(uid_match.group(1)),
        message_id=message_id,
        internal_date=internal_date,
        size_bytes=int(size_match.group(1)) if size_match else None,
        flags=flags,
    )


@contextmanager
def wrap_imap_errors(operation: str = "IMAP operation") -> Iterator[None]:
    """Translate protocol and socket failures into domain exceptions.

    Authentication failures and certificate verification failures are
    permanent. Connection aborts and ordinary network failures are transient.
    Already-translated domain errors pass through unchanged.
    """

    try:
        yield
    except MailDockError:
        raise
    except imaplib.IMAP4.abort as error:
        raise TransientError(f"{operation}: connection aborted") from error
    except (TimeoutError, ConnectionError) as error:
        raise TransientError(f"{operation}: temporary network failure") from error
    except imaplib.IMAP4.error as error:
        message = _exception_text(error)
        if "AUTHENTICATIONFAILED" in message.upper():
            raise AuthenticationError(f"{operation}: authentication failed") from error
        raise PermanentError(f"{operation}: IMAP command failed") from error
    except ssl.SSLError as error:
        if _is_certificate_verification_failure(error):
            raise PermanentError(f"{operation}: certificate verification failed") from error
        raise TransientError(f"{operation}: TLS failure") from error
    except OSError as error:
        classified = classify_os_error(error)
        if classified is not error:
            raise classified from error
        if isinstance(error, (PermissionError, FileNotFoundError)):
            raise PermanentError(f"{operation}: local I/O failure") from error
        raise TransientError(f"{operation}: operating-system I/O failure") from error


def _response_text(response: ImapResponse) -> str:
    if isinstance(response, bytes):
        try:
            return response.decode("ascii")
        except UnicodeDecodeError as error:
            raise PermanentError("IMAP response is not ASCII-compatible") from error
    return response


def _read_imap_token(text: str, position: int) -> tuple[str, int]:
    while position < len(text) and text[position].isspace():
        position += 1
    if position >= len(text):
        raise PermanentError("IMAP response ended before a token")
    if text[position] == '"':
        position += 1
        token: list[str] = []
        while position < len(text):
            character = text[position]
            position += 1
            if character == '"':
                return "".join(token), position
            if character == "\\":
                if position >= len(text):
                    break
                token.append(text[position])
                position += 1
            else:
                token.append(character)
        raise PermanentError("unterminated quoted IMAP token")
    end = position
    while end < len(text) and not text[end].isspace():
        end += 1
    return text[position:end], end


def _fetch_parts(response: FetchResponse) -> tuple[bytes, bytes | None]:
    if isinstance(response, bytes):
        return response, None
    if isinstance(response, tuple) and len(response) == 2:
        metadata, literal = response
        if isinstance(metadata, bytes) and isinstance(literal, bytes):
            return metadata, literal
    for item in response:
        if isinstance(item, tuple) and len(item) == 2:
            metadata, literal = item
            if isinstance(metadata, bytes) and isinstance(literal, bytes):
                return metadata, literal
    raise PermanentError("unsupported IMAP FETCH response shape")


def _message_id_from_headers(headers: bytes | None) -> str | None:
    if not headers:
        return None
    try:
        message = BytesParser(policy=policy.default).parsebytes(headers)
        value = message.get("Message-ID")
    except (ValueError, UnicodeError):
        return None
    return value.strip() if value else None


def _exception_text(error: BaseException) -> str:
    return " ".join(
        argument.decode("utf-8", errors="replace") if isinstance(argument, bytes) else str(argument)
        for argument in error.args
    )


def _is_certificate_verification_failure(error: ssl.SSLError) -> bool:
    return isinstance(error, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in (
        _exception_text(error).upper()
    )
