"""Provider-independent message and storage value objects."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ParsedAttachment:
    filename: str | None
    content_type: str
    size_bytes: int
    is_inline: bool = False


@dataclass(frozen=True)
class ParsedMessage:
    subject: str = ""
    sender: str = ""
    recipient: str = ""
    cc: str = ""
    date_sent: datetime | None = None
    message_id: str | None = None
    in_reply_to: str | None = None
    references_ids: str | None = None
    thread_key: str | None = None
    content_key: str | None = None
    body_text: str = ""
    attachments: tuple[ParsedAttachment, ...] = ()
    has_attachment: bool = False
    parse_error: str | None = None


@dataclass(frozen=True)
class StoredEml:
    relative_path: str
    file_hash: str
    size_bytes: int
    deduplicated: bool = False
