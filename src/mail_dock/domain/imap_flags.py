"""Pure helpers for interpreting stored IMAP flags."""

from __future__ import annotations


def has_imap_flag(flags: str | None, flag: str) -> bool:
    """Return whether ``flag`` occurs as a whitespace-delimited token."""

    expected = flag.casefold()
    return any(token.casefold() == expected for token in (flags or "").split())