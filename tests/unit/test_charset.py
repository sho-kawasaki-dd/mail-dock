import logging

import pytest

from mail_dock.infrastructure.parsing import charset
from mail_dock.infrastructure.parsing.charset import decode_text, normalize_charset_label


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("X-SJIS", "cp932"),
        (" shift_jis ", "cp932"),
        ("sjis", "cp932"),
        ("ISO-2022-JP-MS", "iso2022_jp_ext"),
        (" UTF - 8 ", "utf-8"),
    ],
)
def test_normalize_charset_label(label: str, expected: str) -> None:
    assert normalize_charset_label(label) == expected


def test_decode_text_prefers_declared_charset() -> None:
    raw = "髙".encode("cp932")

    text, encoding = decode_text(raw, "cp932")

    assert text == "髙"
    assert encoding == "cp932"


def test_decode_text_uses_cp932_after_invalid_declared_charset() -> None:
    raw = "①㈱髙".encode("cp932")

    text, encoding = decode_text(raw, "utf-8")

    assert text == "①㈱髙"
    assert encoding == "cp932"


def test_decode_text_uses_charset_normalizer_after_fixed_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Match:
        encoding = "x-test"

        def output(self) -> bytes:
            return "日本語".encode()

    class Result:
        def best(self) -> Match:
            return Match()

    monkeypatch.setattr(charset, "_decode_strict", lambda raw, encoding: None)
    monkeypatch.setattr(charset, "from_bytes", lambda raw: Result())

    raw = b"not-decodable"
    text, encoding = decode_text(raw, None)

    assert text == "日本語"
    assert encoding == "x-test"


def test_decode_text_replaces_invalid_bytes_and_logs_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class NoMatch:
        def best(self) -> None:
            return None

    monkeypatch.setattr(charset, "_decode_strict", lambda raw, encoding: None)
    monkeypatch.setattr(charset, "from_bytes", lambda raw: NoMatch())
    caplog.set_level(logging.WARNING, logger="mail_dock.infrastructure.parsing.charset")

    text, encoding = decode_text(b"\xff\xfe", None)

    assert text == "��"
    assert encoding == "utf-8"
    assert "Unable to determine message charset" in caplog.text
