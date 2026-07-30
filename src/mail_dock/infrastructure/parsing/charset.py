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
_FALLBACK_ENCODINGS = ("iso-2022-jp", "cp932", "euc_jp", "utf-8")


def normalize_charset_label(label: str) -> str:
    """Return a codec-friendly, canonical form of a MIME charset label."""
    normalized = re.sub(r"\s+", "", label.casefold())
    return _CHARSET_ALIASES.get(normalized, normalized)


def _decode_strict(raw: bytes, encoding: str) -> str | None:
    try:
        return raw.decode(encoding, errors="strict")
    except (LookupError, UnicodeDecodeError):
        return None


def decode_text(raw: bytes, declared: str | None) -> tuple[str, str]:
    """Decode bytes and return the text together with the selected encoding.

    The declared charset is authoritative when it can decode the input. The
    remaining attempts follow the fixed order from the Phase 1 specification.
    """
    encodings: list[str] = []
    if declared:
        encodings.append(normalize_charset_label(declared))
    encodings.extend(_FALLBACK_ENCODINGS)

    tried: set[str] = set()
    for encoding in encodings:
        if encoding in tried:
            continue
        tried.add(encoding)
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
