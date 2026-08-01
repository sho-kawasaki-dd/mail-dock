"""Use cases for searching, listing, and retrieving stored messages."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Literal

from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.search import (
    BaseSearchRepository,
    MessageDetail,
    MessageFilter,
    MessageSummary,
    PageCursor,
    SearchPage,
)
from mail_dock.usecases.search_query import parse_query


def search_messages(
    search_repo: BaseSearchRepository,
    *,
    query: str,
    mode: Literal["and", "or"] = "and",
    filters: MessageFilter | None = None,
    cursor: PageCursor | None = None,
    limit: int = 200,
    cancel: CancelToken | None = None,
    on_plan: Callable[[object], None] | None = None,
) -> SearchPage:
    """Search messages after parsing the user query into a search plan."""

    plan = parse_query(query, mode=mode)
    if on_plan is not None:
        on_plan(plan)
    page = search_repo.search_messages(
        plan,
        filters or MessageFilter(),
        cursor=cursor,
        limit=limit,
        cancel=cancel,
    )
    return replace(page, has_slow_path=plan.has_slow_path)


def list_messages(
    search_repo: BaseSearchRepository,
    *,
    filters: MessageFilter | None = None,
    cursor: PageCursor | None = None,
    limit: int = 200,
    cancel: CancelToken | None = None,
) -> SearchPage:
    """List messages using the default active-state filter when unspecified."""

    if cancel is None:
        return search_repo.list_messages(
            filters or MessageFilter(),
            cursor=cursor,
            limit=limit,
        )
    return search_repo.list_messages(
        filters or MessageFilter(),
        cursor=cursor,
        limit=limit,
        cancel=cancel,
    )


def list_thread(
    search_repo: BaseSearchRepository,
    *,
    thread_key: str,
    filters: MessageFilter | None = None,
    cancel: CancelToken | None = None,
) -> Sequence[MessageSummary]:
    """List messages belonging to one thread."""

    if cancel is None:
        return search_repo.list_thread(thread_key, filters or MessageFilter())
    return search_repo.list_thread(thread_key, filters or MessageFilter(), cancel=cancel)


def count_messages(
    search_repo: BaseSearchRepository,
    *,
    query: str | None = None,
    mode: Literal["and", "or"] = "and",
    filters: MessageFilter | None = None,
    cancel: CancelToken | None = None,
) -> int:
    """Count filtered messages, optionally applying a parsed search query."""

    plan = parse_query(query, mode=mode) if query is not None else None
    return search_repo.count_messages(
        filters or MessageFilter(),
        plan,
        cancel=cancel,
    )


def get_message(
    search_repo: BaseSearchRepository,
    *,
    message_id: int,
) -> MessageDetail | None:
    """Return one message detail when it exists."""

    return search_repo.get_message(message_id)
