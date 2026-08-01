"""Provider-independent message and storage value objects."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class ParsedAttachment:
    filename: str | None
    content_type: str
    size_bytes: int
    is_inline: bool = False


@dataclass(frozen=True)
class MessagePart:
    content_id: str | None
    content_type: str
    filename: str | None
    payload: bytes
    is_inline: bool


@dataclass(frozen=True)
class RenderedMessage:
    html_body: str | None
    text_body: str
    parts: tuple[MessagePart, ...]


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


@dataclass(frozen=True)
class AttachmentSavePlan:
    """User-reviewable plan produced before an attachment is written."""

    relative_path: str
    expected_hash: str
    part_index: int
    dest_dir: Path
    filename: str
    warnings: tuple[str, ...]
    is_executable: bool


@dataclass(frozen=True)
class SavedFile:
    """Result of committing an attachment save."""

    path: Path
    warnings: tuple[str, ...]
    is_executable: bool
