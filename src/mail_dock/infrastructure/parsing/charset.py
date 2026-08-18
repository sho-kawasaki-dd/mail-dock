"""Decode MIME text using the mail-dock charset fallback policy."""

from __future__ import annotations

import logging
import re

from charset_normalizer import from_bytes

logger = logging.getLogger(__name__)

_CHARSET_ALIASES = {
    "x-sjis": "cp932",
    "xsjis": "cp932",
    # CP932 extends Shift JIS with Windows and machine-dependent characters.
    "shift_jis": "cp932",
    "shift-jis": "cp932",
    "sjis": "cp932",
    "iso-2022-jp-ms": "iso2022_jp_ext",
    "iso_2022_jp_ms": "iso2022_jp_ext",
    "iso2022jpms": "iso2022_jp_ext",
}
# iso2022_jp_ext covers half-width kana and JIS X 0212 kanji that senders often
# emit while still declaring the plain "iso-2022-jp" label.
_FALLBACK_ENCODINGS = ("iso-2022-jp", "iso2022_jp_ext", "cp932", "euc_jp", "utf-8")


def normalize_charset_label(label: str) -> str:
    """Return a codec-friendly, canonical form of a MIME charset label."""
    normalized = re.sub(r"\s+", "", label.casefold())
    return _CHARSET_ALIASES.get(normalized, normalized)


def _decode_strict(raw: bytes, encoding: str) -> str | None:
    try:
        return raw.decode(encoding, errors="strict")
    except (LookupError, UnicodeDecodeError):
        return None


# Some senders' "iso-2022-jp" bodies contain a handful of NEC-selected IBM
# extended kanji (in-band within the standard ESC $ B designator) that no
# Python stdlib iso2022_jp* codec maps. Accept a lenient decode only when
# almost everything else decoded cleanly, so a genuinely mislabeled cp932
# body (which fails per-byte, not per-character) still falls through instead.
_LENIENT_REPLACEMENT_RATIO_LIMIT = 0.05
_ISO2022_JP_DECLARED_LABELS = frozenset({"iso-2022-jp", "iso2022_jp_ext"})


def _decode_lenient(raw: bytes, encoding: str) -> str | None:
    try:
        text = raw.decode(encoding, errors="replace")
    except LookupError:
        return None
    if not text or text.count("\ufffd") / len(text) > _LENIENT_REPLACEMENT_RATIO_LIMIT:
        return None
    return text


def decode_text(raw: bytes, declared: str | None) -> tuple[str, str]:
    """Decode bytes and return the text together with the selected encoding.

    The declared charset is authoritative when it can decode the input. The
    remaining attempts follow the fixed order from the Phase 1 specification.
    """
    declared_normalized = normalize_charset_label(declared) if declared else None
    encodings: list[str] = []
    if declared_normalized:
        encodings.append(declared_normalized)
    encodings.extend(_FALLBACK_ENCODINGS)

    # cp932 never raises on the 7-bit bytes an ISO-2022-JP body is made of, so
    # it must not be tried before the lenient recovery below gets a chance —
    # otherwise it "succeeds" by leaking every escape sequence verbatim.
    lenient_pending = declared_normalized in _ISO2022_JP_DECLARED_LABELS

    tried: set[str] = set()
    for encoding in encodings:
        if encoding in tried:
            continue
        tried.add(encoding)
        if lenient_pending and encoding not in _ISO2022_JP_DECLARED_LABELS:
            lenient_pending = False
            lenient = _decode_lenient(raw, "iso2022_jp_ext")
            if lenient is not None:
                logger.warning("Replaced untranslatable ISO-2022-JP characters with U+FFFD")
                return lenient, "iso2022_jp_ext (replace)"
        text = _decode_strict(raw, encoding)
        if text is not None:
            return text, encoding

    try:
        detected = from_bytes(raw).best()
        if detected is not None and detected.encoding is not None:
            # CharsetMatch.output() is normalized to UTF-8 by the library.
            return detected.output().decode("utf-8"), detected.encoding
    except Exception as error:
        logger.debug("charset-normalizer could not decode input: %s", error)

    logger.warning("Unable to determine message charset; decoding with replacement")
    return raw.decode("utf-8", errors="replace"), "utf-8"
