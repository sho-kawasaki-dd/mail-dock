from unittest.mock import Mock

import pytest

from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.search import (
    BaseSearchRepository,
    MessageFilter,
    PageCursor,
    SearchPage,
)
from mail_dock.usecases.search_messages import (
    count_messages,
    get_message,
    list_messages,
    list_thread,
    search_messages,
)
from mail_dock.usecases.search_query import parse_query


@pytest.fixture
def search_repo() -> Mock:
    repo = Mock(spec=BaseSearchRepository)
    repo.search_messages.return_value = SearchPage((), None, True)
    repo.list_messages.return_value = SearchPage((), None, True)
    repo.list_thread.return_value = ()
    repo.count_messages.return_value = 0
    repo.get_message.return_value = None
    return repo


def test_search_messages_parses_query_and_applies_default_filter(search_repo: Mock) -> None:
    cancel = CancelToken()
    cursor = PageCursor("2026-07-31T00:00:00+00:00", 42)
    expected_plan = parse_query("alpha beta", mode="or")

    result = search_messages(
        search_repo,
        query="alpha beta",
        mode="or",
        cursor=cursor,
        limit=25,
        cancel=cancel,
    )

    assert result == SearchPage((), None, True)
    search_repo.search_messages.assert_called_once_with(
        expected_plan,
        MessageFilter(),
        cursor=cursor,
        limit=25,
        cancel=cancel,
    )


def test_list_messages_forwards_explicit_filters_and_paging(search_repo: Mock) -> None:
    filters = MessageFilter(account_ids=("account",))
    cursor = PageCursor("", 7)

    list_messages(search_repo, filters=filters, cursor=cursor, limit=10)

    search_repo.list_messages.assert_called_once_with(
        filters,
        cursor=cursor,
        limit=10,
    )


def test_thread_and_detail_operations_are_forwarded(search_repo: Mock) -> None:
    filters = MessageFilter(thread_key="thread")

    assert list_thread(search_repo, thread_key="thread", filters=filters) == ()
    assert get_message(search_repo, message_id=42) is None

    search_repo.list_thread.assert_called_once_with("thread", filters)
    search_repo.get_message.assert_called_once_with(42)


def test_count_messages_without_query_uses_default_filter(search_repo: Mock) -> None:
    cancel = CancelToken()

    assert count_messages(search_repo, cancel=cancel) == 0

    search_repo.count_messages.assert_called_once_with(
        MessageFilter(),
        None,
        cancel=cancel,
    )


def test_count_messages_parses_optional_query(search_repo: Mock) -> None:
    filters = MessageFilter(has_attachment=True)
    expected_plan = parse_query('"invoice 2026"')

    count_messages(
        search_repo,
        query='"invoice 2026"',
        filters=filters,
    )

    search_repo.count_messages.assert_called_once_with(
        filters,
        expected_plan,
        cancel=None,
    )
