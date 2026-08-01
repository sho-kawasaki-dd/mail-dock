"""Qt-independent policy for URLs handed to an external browser."""

from __future__ import annotations

import unicodedata
from urllib.parse import urlsplit

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_MAX_URL_LENGTH = 4096


def _contains_control_character(value: str) -> bool:
    return any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value)


def is_allowed_external_url(url: str) -> bool:
    """Return whether ``url`` may be shown in the external-browser prompt."""
    if len(url) > _MAX_URL_LENGTH or _contains_control_character(url):
        return False

    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return False

    return (
        parsed.scheme.casefold() in _ALLOWED_SCHEMES
        and bool(parsed.netloc)
        and hostname is not None
        and parsed.username is None
        and parsed.password is None
        and (port is None or 0 <= port <= 65535)
    )
