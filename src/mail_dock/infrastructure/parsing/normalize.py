"""Normalize text consistently when indexing and searching.

The same function must always be used when inserting searchable text and when
searching it. Using different normalization rules makes matching fail
permanently. Hiragana and katakana are intentionally kept distinct.
"""

from __future__ import annotations

import re
import unicodedata

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_for_search(text: str) -> str:
    """Return the canonical searchable representation of ``text``."""

    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.casefold()
    normalized = _WHITESPACE_RE.sub(" ", normalized)
    return normalized.strip()