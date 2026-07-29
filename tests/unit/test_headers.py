from datetime import UTC, datetime, timedelta, timezone
from email import policy
from email.message import Message
from email.parser import BytesParser
from pathlib import Path

from mail_dock.infrastructure.parsing.headers import (
    decode_header_value,
    derive_content_key,
    derive_thread_key,
    parse_content_disposition_filename,
    parse_date_header,
    to_utc_iso8601,
)

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "eml"


def _attachment(filename: str) -> Message:
    message = BytesParser(policy=policy.compat32).parsebytes((FIXTURE_ROOT / filename).read_bytes())
    return next(part for part in message.walk() if part.get("Content-Disposition"))


def test_decode_header_value_handles_rfc2047_and_malformed_input() -> None:
    assert decode_header_value("=?UTF-8?B?5pel5pys?=") == "日本"
    malformed = "=?unknown-charset?Q?subject?="
    assert decode_header_value(malformed) == malformed
    assert decode_header_value("=?UTF-8?B?%%%%?=") == "=?UTF-8?B?%%%%?="
    assert decode_header_value(None) == ""


def test_parse_content_disposition_filename_supports_rfc2231_segments() -> None:
    assert parse_content_disposition_filename(_attachment("09_attachment_rfc2231_split.eml")) == (
        "日本語.txt"
    )


def test_parse_content_disposition_filename_supports_outlook_and_name_fallback() -> None:
    assert parse_content_disposition_filename(_attachment("10_attachment_outlook_rfc2047.eml")) == (
        "日本.txt"
    )
    assert parse_content_disposition_filename(_attachment("11_attachment_japanese.eml")) == (
        "請求書.txt"
    )
    part = Message()
    part["Content-Type"] = 'application/octet-stream; name="=?UTF-8?B?5pel5pysLnR4dA==?="'
    assert parse_content_disposition_filename(part) == "日本.txt"


def test_parse_date_header_falls_back_to_internal_date() -> None:
    internal_date = datetime(2026, 7, 30, 1, 2, 3, tzinfo=timezone(timedelta(hours=9)))
    expected = datetime(2026, 7, 29, 16, 2, 3, tzinfo=UTC)
    assert parse_date_header("not a date", internal_date) == expected
    assert parse_date_header(None, internal_date) == expected
    assert parse_date_header("Tue, 30 Jul 2030 12:00:00 +0000", internal_date) == expected


def test_to_utc_iso8601_normalizes_timezone_and_drops_microseconds() -> None:
    value = datetime(2026, 7, 30, 1, 2, 3, 999999, tzinfo=timezone(timedelta(hours=9)))
    assert to_utc_iso8601(value) == "2026-07-29T16:02:03Z"


def test_derive_thread_key_uses_conversation_root_priority() -> None:
    references = "<root@example.test> <reply@example.test>"
    assert derive_thread_key("<self@example.test>", "<reply@example.test>", references) == (
        "<root@example.test>"
    )
    assert derive_thread_key("<self@example.test>", "<reply@example.test>", None) == (
        "<reply@example.test>"
    )
    assert derive_thread_key("<self@example.test>", None, None) == "<self@example.test>"
    assert derive_thread_key(None, None, None) is None


def test_derive_content_key_uses_message_id_or_hash_prefix() -> None:
    digest = "a" * 64
    assert derive_content_key(" <message@example.test> ", digest) == "<message@example.test>"
    assert derive_content_key(None, digest) == f"sha256:{'a' * 32}"
