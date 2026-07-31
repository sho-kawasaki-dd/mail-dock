import json
from datetime import UTC, datetime
from typing import Any, cast

from mail_dock.__main__ import _build_parser, _exit_code, _run_search_command
from mail_dock.domain.errors import SearchQueryError
from mail_dock.domain.search import MessageSummary, PageCursor, SearchPage


class FakeMessageRepository:
    def list_accounts(self) -> list[dict[str, str]]:
        return [{"id": "account-1"}, {"id": "account-2"}]

    def list_folders(self, account_id: str) -> list[dict[str, object]]:
        return [
            {
                "id": 10 if account_id == "account-1" else 20,
                "raw_name": "INBOX",
            },
        ]


class FakeSearchRepository:
    def __init__(self) -> None:
        self.plan: Any = None
        self.filters: Any = None
        self.cursor: PageCursor | None = None
        self.limit = 0

    def search_messages(
        self,
        plan: Any,
        filters: Any,
        *,
        cursor: PageCursor | None = None,
        limit: int = 200,
        cancel: Any = None,
    ) -> SearchPage:
        del cancel
        self.plan = plan
        self.filters = filters
        self.cursor = cursor
        self.limit = limit
        item = MessageSummary(
            id=1,
            account_id="account-1",
            folder_id=10,
            folder_raw_name="INBOX",
            folder_display_name="受信箱",
            subject="件名",
            sender="sender@example.com",
            date_sent=datetime(2026, 1, 2, tzinfo=UTC),
            internal_date=None,
            size_bytes=128,
            has_attachment=False,
            remote_state="present",
            local_state="active",
            thread_key=None,
        )
        return SearchPage(
            items=(item,),
            next_cursor=PageCursor("2026-01-02T00:00:00Z", 1),
            exhausted=False,
        )


def test_search_parser_accepts_e1_options() -> None:
    args = _build_parser().parse_args(
        [
            "search",
            "日本語",
            "--account",
            "account-1",
            "--account",
            "account-2",
            "--folder",
            "INBOX",
            "--since",
            "2026-01-01",
            "--until",
            "2026-01-31",
            "--has-attachment",
            "--mode",
            "or",
            "--limit",
            "25",
            "--after",
            '{"sort_key":"2026-01-01T00:00:00Z","message_id":3}',
            "--json",
        ]
    )

    assert args.command == "search"
    assert args.accounts == ["account-1", "account-2"]
    assert args.folders == ["INBOX"]
    assert args.has_attachment is True
    assert args.mode == "or"
    assert args.limit == 25
    assert args.json is True


def test_search_command_builds_filters_and_prints_json(capsys: Any) -> None:
    args = _build_parser().parse_args(
        [
            "search",
            "短語",
            "--account",
            "account-1",
            "--folder",
            "INBOX",
            "--since",
            "2026-01-01",
            "--until",
            "2026-01-31",
            "--no-attachment",
            "--limit",
            "5",
            "--after",
            '{"sort_key":"2025-12-31T00:00:00Z","message_id":3}',
            "--json",
        ]
    )
    search_repo = FakeSearchRepository()

    result = _run_search_command(
        args,
        cast(Any, FakeMessageRepository()),
        cast(Any, search_repo),
    )

    assert result == 0
    assert search_repo.filters.account_ids == ("account-1",)
    assert search_repo.filters.folder_ids == (10,)
    assert search_repo.filters.has_attachment is False
    assert search_repo.filters.date_from.isoformat() == "2026-01-01T00:00:00+00:00"
    assert search_repo.filters.date_to.isoformat() == "2026-01-31T23:59:59.999999+00:00"
    assert search_repo.limit == 5
    assert search_repo.cursor == PageCursor("2025-12-31T00:00:00Z", 3)

    output = json.loads(capsys.readouterr().out)
    assert output["items"][0]["folder_raw_name"] == "INBOX"
    assert output["items"][0]["folder_display_name"] == "受信箱"
    assert output["next_cursor"] == '{"sort_key":"2026-01-02T00:00:00Z","message_id":1}'


def test_search_command_warns_for_short_terms(capsys: Any) -> None:
    args = _build_parser().parse_args(["search", "短語"])

    _run_search_command(
        args,
        cast(Any, FakeMessageRepository()),
        cast(Any, FakeSearchRepository()),
    )

    captured = capsys.readouterr()
    assert "短い語を含むため時間がかかる場合があります" in captured.err


def test_search_query_error_uses_exit_code_seven() -> None:
    assert _exit_code(SearchQueryError("invalid query")) == 7
