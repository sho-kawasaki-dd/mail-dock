"""Read-only contracts and value objects for searching stored messages.

This module is read-only. Writes belong to ``BaseMessageRepository``; the two
ports are intentionally not combined because their concerns and lifecycles
are different.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from mail_dock.domain.fetcher import CancelToken


@dataclass(frozen=True)
class MessageFilter:
    """Structured filters applied to message search and listing queries."""

    account_ids: tuple[str, ...] | None = None
    folder_ids: tuple[int, ...] | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    has_attachment: bool | None = None
    local_states: frozenset[str] = field(default_factory=lambda: frozenset({"active"}))
    remote_states: frozenset[str] | None = None
    thread_key: str | None = None


@dataclass(frozen=True)
class PageCursor:
    """Opaque keyset cursor containing the last returned sort tuple."""

    sort_key: str
    message_id: int

    def to_string(self) -> str:
        """Serialize this cursor for transport through the CLI."""

        return json.dumps(
            {"sort_key": self.sort_key, "message_id": self.message_id},
            ensure_ascii=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_string(cls, value: str) -> PageCursor:
        """Restore a cursor serialized by :meth:`to_string`.

        ``ValueError`` is used for malformed cursors because a cursor is
        command-line input, not a database or transport failure.
        """

        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid page cursor") from exc

        if not isinstance(decoded, dict):
            raise ValueError("invalid page cursor")
        sort_key = decoded.get("sort_key")
        message_id = decoded.get("message_id")
        if not isinstance(sort_key, str) or type(message_id) is not int:
            raise ValueError("invalid page cursor")
        return cls(sort_key=sort_key, message_id=message_id)


@dataclass(frozen=True)
class MessageSummary:
    """The fields needed to render one row in the message list."""

    id: int
    account_id: str
    folder_id: int
    folder_raw_name: str
    folder_display_name: str
    subject: str
    sender: str
    date_sent: datetime | None
    internal_date: datetime | None
    size_bytes: int | None
    has_attachment: bool
    remote_state: str
    local_state: str
    thread_key: str | None
    imap_flags: str | None
    moved_to_folder_display_name: str | None
    failure_class: str | None
    flags_seen_at: datetime | None


@dataclass(frozen=True)
class MessageDetail(MessageSummary):
    """A message summary plus headers and the stored EML location."""

    recipient: str
    cc: str
    message_id: str | None
    in_reply_to: str | None
    references_ids: str | None
    relative_path: str | None
    file_hash: str | None
    imap_flags: str | None


@dataclass(frozen=True)
class SearchPage:
    """One keyset-paginated result page."""

    items: tuple[MessageSummary, ...]
    next_cursor: PageCursor | None
    exhausted: bool
    has_slow_path: bool = False


@dataclass(frozen=True)
class SearchPlan:
    """Normalized terms and their execution paths for a search query.

    Every term is normalized before it is stored here. The concrete repository
    decides how the MATCH and LIKE paths are combined.
    """

    match_terms: tuple[str, ...] = ()
    like_terms: tuple[str, ...] = ()
    exclude_match_terms: tuple[str, ...] = ()
    exclude_like_terms: tuple[str, ...] = ()
    mode: Literal["and", "or"] = "and"
    has_slow_path: bool = False


class BaseSearchRepository(ABC):
    """Read-only port for message search, listing, and detail retrieval."""

    @abstractmethod
    def search_messages(
        self,
        plan: SearchPlan,
        filters: MessageFilter,
        *,
        cursor: PageCursor | None = None,
        limit: int = 200,
        cancel: CancelToken | None = None,
    ) -> SearchPage:
        """Search messages using a parsed plan and structured filters."""

    @abstractmethod
    def list_messages(
        self,
        filters: MessageFilter,
        *,
        cursor: PageCursor | None = None,
        limit: int = 200,
        cancel: CancelToken | None = None,
    ) -> SearchPage:
        """List messages using the same filtering and paging contract."""

    @abstractmethod
    def count_messages(
        self,
        filters: MessageFilter,
        plan: SearchPlan | None = None,
        *,
        cancel: CancelToken | None = None,
    ) -> int:
        """Count messages matching optional search terms and filters."""

    @abstractmethod
    def list_thread(
        self,
        thread_key: str,
        filters: MessageFilter,
        *,
        cancel: CancelToken | None = None,
    ) -> Sequence[MessageSummary]:
        """List messages in one thread using the supplied filters."""

    @abstractmethod
    def get_message(self, message_id: int) -> MessageDetail | None:
        """Return one message detail, or ``None`` when it does not exist."""
