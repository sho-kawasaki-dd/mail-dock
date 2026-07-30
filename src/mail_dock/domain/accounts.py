"""Pure validation rules for account identifiers used as storage paths."""

from __future__ import annotations

import re

_ACCOUNT_ID_FORBIDDEN = re.compile(r'[<>:"/\\|?*]')
_ACCOUNT_ID_RESERVED = re.compile(
    r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$", re.IGNORECASE
)


def validate_account_id(account_id: str) -> None:
    """Reject account IDs that could create an unsafe Windows path."""

    if not account_id or account_id in {".", ".."}:
        raise ValueError("account_id must not be empty or a path component")
    if any(ord(character) < 32 for character in account_id):
        raise ValueError("account_id must not contain control characters")
    if _ACCOUNT_ID_FORBIDDEN.search(account_id):
        raise ValueError("account_id contains a forbidden path character")
    if account_id.endswith((".", " ")):
        raise ValueError("account_id must not end with a dot or space")
    if _ACCOUNT_ID_RESERVED.fullmatch(account_id):
        raise ValueError("account_id is a reserved Windows device name")
    if len(account_id.encode("utf-8")) > 255:
        raise ValueError("account_id is too long")
