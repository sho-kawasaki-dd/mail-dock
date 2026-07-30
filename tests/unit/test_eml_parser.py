from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from mail_dock.domain.messages import ParsedMessage
from mail_dock.infrastructure.parsing.eml_parser import parse_eml

FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "eml"
INTERNAL_DATE = datetime(2026, 7, 30, 12, tzinfo=UTC)


def _parse_fixture(name: str) -> ParsedMessage:
    return parse_eml((FIXTURE_DIR / name).read_bytes(), INTERNAL_DATE)


def test_corpus_is_safe_to_parse() -> None:
    parsed = {
        path.name: parse_eml(path.read_bytes(), INTERNAL_DATE)
        for path in sorted(FIXTURE_DIR.glob("*.eml"))
    }

    assert len(parsed) == 25
    assert parsed["24_malformed_boundary.eml"].parse_error is not None
    assert parsed["25_malformed_base64.eml"].parse_error is not None
    assert all(
        parsed[name].parse_error is None
        for name in parsed
        if name not in {"24_malformed_boundary.eml", "25_malformed_base64.eml"}
    )


def test_plain_text_is_preferred_to_html() -> None:
    parsed = _parse_fixture("15_multipart_alternative.eml")

    assert parsed.body_text == "Plain text wins."


def test_related_body_and_inline_image_are_separated() -> None:
    parsed = _parse_fixture("16_multipart_related_inline_image.eml")

    assert parsed.body_text == "Inline image"
    assert parsed.has_attachment is False
    assert len(parsed.attachments) == 1
    assert parsed.attachments[0].filename == "logo.png"
    assert parsed.attachments[0].is_inline is True


def test_nested_body_and_regular_attachment_are_extracted() -> None:
    parsed = _parse_fixture("17_nested_multipart.eml")

    assert parsed.body_text == "Nested plain body."
    assert parsed.has_attachment is True
    assert [attachment.filename for attachment in parsed.attachments] == ["nested.txt"]


def test_attachment_only_message_has_no_body() -> None:
    parsed = _parse_fixture("18_attachment_only.eml")

    assert parsed.body_text == ""
    assert parsed.has_attachment is True
    assert parsed.attachments[0].filename == "report.txt"


def test_headers_and_derived_keys_are_populated() -> None:
    parsed = _parse_fixture("19_message_id_missing.eml")

    assert parsed.sender == "sender@example.test"
    assert parsed.recipient == "recipient@example.test"
    assert parsed.message_id is None
    assert parsed.thread_key is None
    assert parsed.content_key is not None
    assert parsed.content_key.startswith("sha256:")


def test_invalid_and_future_dates_fall_back_to_internal_date() -> None:
    invalid = _parse_fixture("21_date_invalid.eml")
    missing = _parse_fixture("22_date_missing.eml")
    future = _parse_fixture("23_date_future.eml")

    assert invalid.date_sent == INTERNAL_DATE
    assert missing.date_sent == INTERNAL_DATE
    assert future.date_sent == INTERNAL_DATE
