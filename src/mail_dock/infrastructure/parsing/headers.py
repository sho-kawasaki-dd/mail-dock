"""Decode message headers and derive stable metadata for stored messages."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Final
from urllib.parse import unquote_to_bytes

from mail_dock.domain.normalize import replace_surrogates

_PARAMETER_NAME: Final[re.Pattern[str]] = re.compile(
    r"^(?P<name>[^*]+)(?:\*(?P<index>\d+)(?P<encoded>\*)?|(?P<extended>\*))?$",
    re.IGNORECASE,
)
_MESSAGE_ID: Final[re.Pattern[str]] = re.compile(r"<[^>]+>")


def decode_header_value(value: str | None) -> str:
    """Decode an RFC 2047 header, retaining the source on malformed input."""
    if value is None:
        return ""

    try:
        decoded = str(make_header(decode_header(value)))
        return replace_surrogates(value if value and not decoded else decoded)
    except (LookupError, UnicodeError, ValueError):
        return replace_surrogates(value)


def _raw_header_value(part: Message, header_name: str) -> str | None:
    header_name_lower = header_name.lower()
    for name, value in part.raw_items():
        if name.lower() == header_name_lower:
            return value
    fallback_value = part.get(header_name)
    return str(fallback_value) if fallback_value is not None else None


def _split_parameters(value: str) -> list[str]:
    parameters: list[str] = []
    start = 0
    quoted = False
    escaped = False

    for index, character in enumerate(value):
        if escaped:
            escaped = False
        elif character == "\\" and quoted:
            escaped = True
        elif character == '"':
            quoted = not quoted
        elif character == ";" and not quoted:
            parameters.append(value[start:index])
            start = index + 1

    parameters.append(value[start:])
    return parameters


def _unquote_parameter(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1]

    result: list[str] = []
    escaped = False
    for character in value:
        if escaped:
            result.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        else:
            result.append(character)
    if escaped:
        result.append("\\")
    return "".join(result)


def _parse_parameters(value: str) -> dict[str, str]:
    parameters: dict[str, str] = {}
    for item in _split_parameters(value)[1:]:
        name, separator, parameter_value = item.partition("=")
        if separator:
            parameters[name.strip().lower()] = _unquote_parameter(parameter_value)
    return parameters


def _decode_rfc2231_value(value: str, *, encoded: bool) -> str:
    if not encoded:
        return value

    charset, separator, encoded_value = value.partition("'")
    if separator:
        _, separator, encoded_value = encoded_value.partition("'")
        if not separator:
            encoded_value = value
    else:
        charset = "utf-8"
        encoded_value = value

    raw_value = unquote_to_bytes(encoded_value)
    try:
        return raw_value.decode(charset or "utf-8")
    except (LookupError, UnicodeDecodeError):
        return raw_value.decode("utf-8", errors="replace")


def _filename_from_parameters(parameters: dict[str, str], base_name: str) -> str | None:
    extended_value = parameters.get(f"{base_name}*")
    if extended_value is not None:
        return _decode_rfc2231_value(extended_value, encoded=True)

    segments: list[tuple[int, str, bool]] = []
    for parameter_name, parameter_value in parameters.items():
        match = _PARAMETER_NAME.fullmatch(parameter_name)
        if match is None or match.group("name").lower() != base_name:
            continue
        index = match.group("index")
        if index is not None:
            segments.append((int(index), parameter_value, match.group("encoded") is not None))

    if not segments:
        simple_value = parameters.get(base_name)
        return decode_header_value(simple_value) if simple_value is not None else None

    segments.sort(key=lambda segment: segment[0])
    if segments[0][0] != 0:
        return None
    decoded_segments: list[str] = []
    for expected_index, (index, segment, encoded) in enumerate(segments):
        if index != expected_index:
            break
        decoded_segments.append(_decode_rfc2231_value(segment, encoded=encoded))
    return "".join(decoded_segments) or None


def parse_content_disposition_filename(part: Message) -> str | None:
    """Return a decoded filename from Content-Disposition or Content-Type."""
    disposition = _raw_header_value(part, "Content-Disposition")
    content_type = _raw_header_value(part, "Content-Type")

    for header_value in (disposition, content_type):
        if header_value is None:
            continue
        parameters = _parse_parameters(header_value)
        filename = _filename_from_parameters(parameters, "filename")
        if filename is not None:
            return replace_surrogates(filename)
        filename = _filename_from_parameters(parameters, "name")
        if filename is not None:
            return replace_surrogates(filename)
    return None


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_date_header(value: str | None, internal_date: datetime | None) -> datetime | None:
    """Parse Date and use INTERNALDATE for invalid or implausibly future values."""
    fallback = _as_utc(internal_date)
    if not value:
        return fallback

    try:
        parsed_value = parsedate_to_datetime(value)
        parsed = _as_utc(parsed_value)
    except (TypeError, ValueError, OverflowError):
        return fallback

    if parsed is None:
        return fallback
    if parsed > datetime.now(UTC) + timedelta(days=1):
        return fallback
    return parsed


def to_utc_iso8601(value: datetime) -> str:
    """Format a datetime as a UTC ISO 8601 timestamp without fractional seconds."""
    utc_value = _as_utc(value)
    if utc_value is None:
        raise ValueError("datetime value is required")
    return (
        f"{utc_value.year:04d}-{utc_value.month:02d}-{utc_value.day:02d}"
        f"T{utc_value.hour:02d}:{utc_value.minute:02d}:{utc_value.second:02d}Z"
    )


def _first_message_id(value: str | None) -> str | None:
    if not value:
        return None
    match = _MESSAGE_ID.search(value)
    return match.group(0) if match is not None else value.split()[0]


def derive_thread_key(
    message_id: str | None,
    in_reply_to: str | None,
    references_ids: str | None,
) -> str | None:
    """Choose the conversation root from References, In-Reply-To, or Message-ID."""
    return (
        _first_message_id(references_ids)
        or _first_message_id(in_reply_to)
        or _first_message_id(message_id)
    )


def derive_content_key(message_id: str | None, eml_sha256: str) -> str:
    """Derive a stable content key from Message-ID or the EML hash."""
    normalized_message_id = message_id.strip() if message_id else ""
    if normalized_message_id:
        return normalized_message_id
    return f"sha256:{eml_sha256[:32]}"
