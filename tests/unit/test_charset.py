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


def test_decode_text_uses_iso2022_jp_ext_for_halfwidth_kana_mislabeled_as_plain() -> None:
    # Real senders emit half-width kana (ESC ( I) while still declaring plain
    # "iso-2022-jp", which the strict codec rejects with UnicodeDecodeError.
    raw = b"ABC\x1b(I\x31\x32\x33\x1b(B"

    text, encoding = decode_text(raw, "iso-2022-jp")

    assert text == "ABCｱｲｳ"
    assert encoding == "iso2022_jp_ext"


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


def test_decode_text_replaces_untranslatable_nec_ibm_extension_char() -> None:
    # ku 89 / ten 85 ("yu") is an NEC-selected IBM extended kanji that no
    # stdlib iso2022_jp* codec maps, verified against a real production email.
    # A realistically long body keeps the replacement ratio under the limit.
    body = "いつもお世話になっております。日本語のご案内メールです。" * 3
    encoded = body.encode("iso2022_jp_ext")
    raw = encoded.removesuffix(b"\x1b(B") + b"yu" + b"\x1b(B"

    text, encoding = decode_text(raw, "iso-2022-jp")

    assert text == body + "\ufffd"
    assert encoding == "iso2022_jp_ext (replace)"


def test_decode_text_still_falls_back_to_cp932_when_mostly_undecodable() -> None:
    # A genuinely mislabeled cp932 body must not be shredded into mostly "�".
    raw = "①㈱髙".encode("cp932")

    text, encoding = decode_text(raw, "iso-2022-jp")

    assert text == "①㈱髙"
    assert encoding == "cp932"


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
