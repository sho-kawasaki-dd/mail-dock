from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mail_dock.domain.errors import OperationCancelledError
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.search import MessageFilter, PageCursor
from mail_dock.infrastructure.database.message_repository import SqliteMessageRepository
from mail_dock.infrastructure.database.migrator import migrate
from mail_dock.infrastructure.database.search_repository import SqliteSearchRepository
from mail_dock.usecases.search_messages import (
    count_messages,
    get_message,
    list_messages,
    list_thread,
    search_messages,
)


class _ProgressCancelToken(CancelToken):
    def __init__(self) -> None:
        super().__init__()
        self.checks = 0

    @property
    def is_cancelled(self) -> bool:
        self.checks += 1
        return self.checks >= 3


def _repositories(
    connection: sqlite3.Connection, db_path: Path
) -> tuple[SqliteMessageRepository, SqliteSearchRepository, int, int]:
    migrate(connection, db_path)
    messages = SqliteMessageRepository(connection)
    messages.upsert_account({"id": "account-a", "provider_type": "imap"})
    messages.upsert_account({"id": "account-b", "provider_type": "imap"})
    folder_a = messages.upsert_folder(
        {"account_id": "account-a", "raw_name": "INBOX", "display_name": "受信箱"}
    )
    folder_b = messages.upsert_folder(
        {"account_id": "account-b", "raw_name": "Archive", "display_name": "保存"}
    )
    return messages, SqliteSearchRepository(connection), folder_a, folder_b


def _add(
    repository: SqliteMessageRepository,
    folder_id: int,
    uid: int,
    *,
    account_id: str = "account-a",
    subject: str = "subject",
    sender: str = "sender@example.com",
    body: str = "body",
    attachment_names: str | None = None,
    date_sent: str | None = "2026-07-31T12:00:00Z",
    internal_date: str | None = "2026-07-31T11:00:00Z",
    has_attachment: int = 0,
    local_state: str = "active",
    remote_state: str = "present",
    thread_key: str | None = None,
    uidvalidity: int = 1,
    imap_flags: str | None = "\\Seen",
) -> int:
    return int(
        repository.add_message(
            {
                "account_id": account_id,
                "folder_id": folder_id,
                "uid": uid,
                "uidvalidity": uidvalidity,
                "content_key": f"content-{account_id}-{uid}",
                "source_item_key": f"source-{account_id}-{uid}",
                "subject": subject,
                "sender": sender,
                "date_sent": date_sent,
                "internal_date": internal_date,
                "has_attachment": has_attachment,
                "local_state": local_state,
                "remote_state": remote_state,
                "thread_key": thread_key,
                "recipient": "recipient@example.com",
                "cc": "cc@example.com",
                "message_id": f"<message-{uid}@example.com>",
                "in_reply_to": "<parent@example.com>",
                "references_ids": "<parent@example.com>",
                "relative_path": f"eml/{uid}.eml",
                "file_hash": f"hash-{uid}",
                "imap_flags": imap_flags,
            },
            {
                "subject": subject,
                "sender": sender,
                "body_text": body,
                "attachment_names": attachment_names,
            },
        )
    )


def test_match_like_and_attachment_name_search(db_conn: sqlite3.Connection, tmp_path: Path) -> None:
    messages, search, folder_a, _ = _repositories(db_conn, tmp_path / "metadata.db")
    match_id = _add(messages, folder_a, 1, subject="Invoice 2026", body="long body")
    like_id = _add(
        messages,
        folder_a,
        2,
        subject="Other",
        body="two character marker",
        attachment_names="report.xlsx",
        has_attachment=1,
    )
    _add(messages, folder_a, 3, subject="Unrelated", body="nothing")

    assert [item.id for item in search_messages(search, query="invoice").items] == [match_id]
    assert [item.id for item in search_messages(search, query="tw").items] == [like_id]
    assert [item.id for item in search_messages(search, query="xlsx").items] == [like_id]


def test_search_query_values_are_parameters_not_sql(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    messages, search, folder_a, _ = _repositories(db_conn, tmp_path / "metadata.db")
    _add(messages, folder_a, 1, subject="ordinary")

    assert search_messages(search, query="'; DROP TABLE messages; --").items == ()
    assert db_conn.execute("SELECT COUNT(*) FROM messages").fetchone() == (1,)


def test_and_or_and_exclusion_are_combined(db_conn: sqlite3.Connection, tmp_path: Path) -> None:
    messages, search, folder_a, _ = _repositories(db_conn, tmp_path / "metadata.db")
    both_id = _add(messages, folder_a, 1, subject="alpha beta")
    alpha_id = _add(messages, folder_a, 2, subject="alpha only")
    beta_id = _add(messages, folder_a, 3, subject="beta only")

    assert {item.id for item in search_messages(search, query="alpha beta").items} == {both_id}
    assert {item.id for item in search_messages(search, query="alpha beta", mode="or").items} == {
        both_id,
        alpha_id,
        beta_id,
    }
    assert [item.id for item in search_messages(search, query="alpha -beta").items] == [alpha_id]


def test_structured_filters_and_default_states(db_conn: sqlite3.Connection, tmp_path: Path) -> None:
    messages, search, folder_a, folder_b = _repositories(db_conn, tmp_path / "metadata.db")
    active = _add(
        messages,
        folder_a,
        1,
        subject="needle",
        has_attachment=1,
        date_sent="2026-07-30T12:00:00Z",
        remote_state="deleted",
    )
    _add(messages, folder_b, 2, account_id="account-b", subject="needle")
    _add(messages, folder_a, 3, subject="needle", local_state="purged")
    _add(messages, folder_a, 4, subject="needle", local_state="trashed")

    filters = MessageFilter(
        account_ids=("account-a",),
        folder_ids=(folder_a,),
        date_from=datetime(2026, 7, 30, tzinfo=UTC),
        date_to=datetime(2026, 7, 30, 23, 59, tzinfo=UTC),
        has_attachment=True,
        remote_states=frozenset({"deleted"}),
    )
    assert [item.id for item in search_messages(search, query="needle", filters=filters).items] == [
        active
    ]
    assert [item.id for item in search_messages(search, query="needle").items] == [2, active]


def test_keyset_paging_handles_ties_and_null_dates(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    messages, search, folder_a, _ = _repositories(db_conn, tmp_path / "metadata.db")
    ids = [
        _add(messages, folder_a, 1, date_sent="2026-07-31T12:00:00Z"),
        _add(messages, folder_a, 2, date_sent="2026-07-31T12:00:00Z"),
        _add(messages, folder_a, 3, date_sent=None, internal_date="2026-07-30T12:00:00Z"),
        _add(messages, folder_a, 4, date_sent=None, internal_date=None),
    ]

    first = list_messages(search, limit=2)
    second = list_messages(search, cursor=first.next_cursor, limit=2)

    assert first.next_cursor == PageCursor("2026-07-31T12:00:00Z", ids[0])
    assert [item.id for item in first.items + second.items] == [ids[1], ids[0], ids[2], ids[3]]
    assert second.next_cursor is None
    assert second.exhausted is True


def test_list_summary_includes_joined_status_fields_for_current_uidvalidity(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    messages, search, folder_a, _ = _repositories(db_conn, tmp_path / "metadata.db")
    moved_folder = messages.upsert_folder(
        {"account_id": "account-a", "raw_name": "Moved", "display_name": "移動先"}
    )
    moved_id = _add(messages, folder_a, 1, imap_flags="\\Flagged")
    failed_id = _add(messages, folder_a, 2)
    messages.update_remote_state(moved_id, "moved", moved_folder)
    messages.record_failure("account-a", folder_a, 1, 2, "oversize", "too large")
    messages.record_failure("account-a", folder_a, 99, 2, "parse", "old generation")

    items = {item.id: item for item in list_messages(search).items}

    assert items[moved_id].imap_flags == "\\Flagged"
    assert items[moved_id].moved_to_folder_display_name == "移動先"
    assert items[moved_id].failure_class is None
    assert items[failed_id].failure_class == "oversize"


def test_list_page_uses_one_joined_select_without_detail_queries(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    messages, search, folder_a, _ = _repositories(db_conn, tmp_path / "metadata.db")
    _add(messages, folder_a, 1)
    statements: list[str] = []
    db_conn.set_trace_callback(statements.append)
    try:
        list_messages(search)
    finally:
        db_conn.set_trace_callback(None)

    select_statements = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
    ]
    assert len(select_statements) == 1
    assert "sync_failures" in select_statements[0]


def test_read_operations_return_count_thread_and_detail(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    messages, search, folder_a, _ = _repositories(db_conn, tmp_path / "metadata.db")
    first = _add(messages, folder_a, 1, subject="thread one", thread_key="thread")
    second = _add(messages, folder_a, 2, subject="thread two", thread_key="thread")

    assert count_messages(search, query="thread") == 2
    assert [item.id for item in list_thread(search, thread_key="thread")] == [second, first]
    detail = get_message(search, message_id=first)
    assert detail is not None
    assert detail.recipient == "recipient@example.com"
    assert detail.relative_path == "eml/1.eml"
    assert get_message(search, message_id=9999) is None


def test_cancelled_search_is_reported_as_operation_cancelled(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    messages, search, folder_a, _ = _repositories(db_conn, tmp_path / "metadata.db")
    _add(messages, folder_a, 1, subject="needle", body="needle")
    cancel = CancelToken()
    cancel.cancel()

    with pytest.raises(OperationCancelledError):
        search_messages(search, query="ne", cancel=cancel)


def test_like_scan_can_be_interrupted_by_progress_handler(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    messages, search, folder_a, _ = _repositories(db_conn, tmp_path / "metadata.db")
    for uid in range(1, 501):
        _add(messages, folder_a, uid, body="needle " * 500)

    with pytest.raises(OperationCancelledError):
        search_messages(search, query="ne", cancel=_ProgressCancelToken())
