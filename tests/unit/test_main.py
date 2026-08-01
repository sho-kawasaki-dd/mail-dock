import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

import mail_dock.__main__ as main
from mail_dock import config
from mail_dock.__main__ import StorageSession, _build_parser, _exit_code, _run_search_command
from mail_dock.domain.errors import SearchQueryError
from mail_dock.domain.search import MessageSummary, PageCursor, SearchPage
from mail_dock.infrastructure.storage.storage_root import StorageLock


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
            imap_flags="\\Seen",
            moved_to_folder_display_name=None,
            failure_class=None,
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


def test_parser_accepts_gui_command() -> None:
    args = _build_parser().parse_args(["gui"])

    assert args.command == "gui"


def test_main_routes_gui_and_no_command_without_starting_storage_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = config.AppConfig()
    calls: list[Path | None] = []

    monkeypatch.setattr(config, "load", lambda: settings)
    monkeypatch.setattr(main, "setup_logging", lambda *args, **kwargs: None)

    def fake_run_gui(received_settings: config.AppConfig, requested_root: Path | None) -> int:
        calls.append(requested_root)
        return 0 if received_settings is settings else 1

    monkeypatch.setattr(
        main,
        "_run_gui",
        fake_run_gui,
    )

    assert main.main([]) == 0
    assert main.main(["gui", "--storage-root", "/tmp/mail-dock-test"]) == 0
    assert calls == [None, Path("/tmp/mail-dock-test")]


def test_main_keeps_existing_cli_commands_on_the_cli_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = config.AppConfig()
    calls: list[tuple[str | None, object]] = []

    monkeypatch.setattr(config, "load", lambda: settings)
    monkeypatch.setattr(main, "setup_logging", lambda *args, **kwargs: None)

    def fake_run_command(
        received_settings: config.AppConfig,
        requested_root: Path | None,
        command: str | None,
        args: object,
    ) -> int:
        assert received_settings is settings
        calls.append((command, args))
        return 17

    monkeypatch.setattr(main, "_run_command", fake_run_command)

    assert main.main(["migrate"]) == 17
    assert main.main(["verify", "--storage-root", "/tmp/mail-dock-test"]) == 17

    assert [command for command, _ in calls] == ["migrate", "verify"]


def test_storage_session_migrates_saves_settings_and_releases_lock(
    tmp_storage_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = config.AppConfig()
    saved: list[config.AppConfig] = []

    def save_settings(value: config.AppConfig) -> None:
        saved.append(value)

    monkeypatch.setattr(config, "save", save_settings)
    monkeypatch.setattr(main, "check_free_space", lambda path: None)

    with StorageSession(settings, tmp_storage_root) as session:
        assert session.root == tmp_storage_root
        assert (
            session.connection_manager.get_connection().execute("PRAGMA user_version").fetchone()[0]
            == 3
        )

    assert saved[0].storage_root_candidates == (str(tmp_storage_root.resolve()),)
    assert saved[0].storage_root_uuid is not None
    with StorageLock(tmp_storage_root) as lock:
        assert lock.held


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


class FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_verify_fts_uses_and_closes_writable_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = FakeConnection()
    connect_calls: list[tuple[Path, bool, bool]] = []
    integrity_calls: list[FakeConnection] = []

    def fake_connect(
        db_path: Path,
        *,
        readonly: bool = False,
        network_drive: bool = False,
    ) -> FakeConnection:
        connect_calls.append((db_path, readonly, network_drive))
        return connection

    def fake_integrity_check(value: FakeConnection) -> None:
        integrity_calls.append(value)

    monkeypatch.setattr(main, "connect", fake_connect)
    monkeypatch.setattr(main, "integrity_check", fake_integrity_check)

    main._verify_fts_database(Path("metadata.db"), network_drive=True)

    assert connect_calls == [(Path("metadata.db"), False, True)]
    assert integrity_calls == [connection]
    assert connection.closed


def test_verify_fts_closes_connection_when_check_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()

    monkeypatch.setattr(main, "connect", lambda *args, **kwargs: connection)

    def fail_integrity_check(value: FakeConnection) -> None:
        del value
        raise RuntimeError("check failed")

    monkeypatch.setattr(main, "integrity_check", fail_integrity_check)

    with pytest.raises(RuntimeError, match="check failed"):
        main._verify_fts_database(Path("metadata.db"), network_drive=False)

    assert connection.closed
