"""Stable remote identity keys shared by synchronization and reindexing."""

from __future__ import annotations

import base64


def imap_folder_key(folder_raw_name: str) -> str:
    """Encode an IMAP folder name reversibly for an account-scoped identity key."""

    if not folder_raw_name:
        raise ValueError("folder_raw_name must not be empty")
    encoded = base64.urlsafe_b64encode(folder_raw_name.encode("utf-8", "surrogatepass"))
    return encoded.rstrip(b"=").decode("ascii")


def imap_source_item_key(folder_raw_name: str, uidvalidity: int, uid: int) -> str:
    if uidvalidity < 0 or uid < 1:
        raise ValueError("uidvalidity and uid must be non-negative and positive respectively")
    return f"imap:{imap_folder_key(folder_raw_name)}:{uidvalidity}:{uid}"


def gmail_source_item_key(gmail_msgid: str) -> str:
    if not gmail_msgid.isdecimal():
        raise ValueError("gmail_msgid must be a decimal string")
    return f"gmail:{gmail_msgid}"


def remote_source_item_key(
    folder_raw_name: str,
    uidvalidity: int,
    uid: int,
    gmail_msgid: str | None = None,
) -> str:
    if gmail_msgid is not None:
        return gmail_source_item_key(gmail_msgid)
    return imap_source_item_key(folder_raw_name, uidvalidity, uid)
