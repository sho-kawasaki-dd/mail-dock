from __future__ import annotations

from pathlib import Path

import pytest

from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.normalize import normalize_for_search
from mail_dock.domain.search import MessageFilter
from mail_dock.infrastructure.database.search_repository import SqliteSearchRepository
from mail_dock.infrastructure.storage.eml_storage import EmlStorage
from mail_dock.infrastructure.storage.manifest import ManifestWriter
from mail_dock.infrastructure.storage.storage_root import initialize_root
from mail_dock.usecases.search_messages import search_messages
from mail_dock.usecases.search_query import parse_query
from mail_dock.usecases.sync_mail import SyncOptions, sync_account
from tests.support.eml_builder import AttachmentSpec, build_eml, build_related_email
from tests.support.imap_integration import (
    append_raw_message,
    create_mailbox,
    imap_client,
    make_fetcher,
    open_repository,
    register_account_and_folder,
    service,
    unique_mailbox,
)

SEARCHABLE_SUBJECT = "Fullwidth \uff34\uff25\uff33\uff34 subject"
FULLWIDTH_SEARCH = "\uff33\uff25\uff21\uff32\uff23\uff28"


@pytest.mark.docker
def test_sync_then_search_handles_normalization_charsets_and_attachments(
    tmp_path: Path,
) -> None:
    settings = service("dovecot")
    mailbox = unique_mailbox("SearchFlow")
    searchable_body = "日本語の本文と CP932 の①㈱髙"
    regular_attachment = "請求書_2026.xlsx"
    inline_filename = "inline-only-logo.png"

    searchable = build_eml(
        subject=SEARCHABLE_SUBJECT,
        sender="Search Sender <SEARCH@example.test>",
        body=searchable_body,
        charset="cp932",
        attachments=(
            AttachmentSpec(
                regular_attachment,
                b"spreadsheet",
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
        ),
    )
    hiragana = build_eml(
        subject="Hiragana message",
        sender="hiragana@example.invalid",
        body="かなだけの本文",
    )
    katakana = build_eml(
        subject="Katakana message",
        sender="katakana@example.invalid",
        body="カナだけの本文",
    )
    inline = build_related_email(
        body_html='<p>Inline body</p><img src="cid:inline-image">',
        cid="inline-image",
        filename=inline_filename,
    )
    with imap_client(settings) as client:
        create_mailbox(client, mailbox)
        append_raw_message(client, mailbox, searchable)
        append_raw_message(client, mailbox, hiragana)
        append_raw_message(client, mailbox, katakana)
        append_raw_message(client, mailbox, inline)

    account_id = "integration-search"
    repository, connection = open_repository(tmp_path)
    root = tmp_path / "storage"
    initialize_root(root)
    register_account_and_folder(repository, account_id, mailbox)
    storage = EmlStorage(root)
    manifest = ManifestWriter(root, account_id)
    fetcher = make_fetcher(settings)
    try:
        fetcher.connect()
        result = sync_account(
            fetcher,
            repository,
            storage,
            manifest,
            account_id=account_id,
            options=SyncOptions(),
            cancel=CancelToken(),
        )
    finally:
        fetcher.disconnect()
        manifest.close()

    assert result.fetched_count == 4
    search = SqliteSearchRepository(connection)

    assert _subjects(search, "日本語") == {SEARCHABLE_SUBJECT}
    assert _subjects(search, FULLWIDTH_SEARCH) == {SEARCHABLE_SUBJECT}
    assert _subjects(search, "CP932") == {SEARCHABLE_SUBJECT}
    assert _subjects(search, "髙") == {SEARCHABLE_SUBJECT}
    assert _subjects(search, "かな") == {"Hiragana message"}
    assert _subjects(search, "カナ") == {"Katakana message"}
    assert _subjects(search, "なだ") == {"Hiragana message"}
    assert _subjects(search, "請求書") == {SEARCHABLE_SUBJECT}
    assert _subjects(search, "xlsx") == {SEARCHABLE_SUBJECT}
    assert _subjects(search, inline_filename) == set()
    assert _subjects(search, "本") == {
        SEARCHABLE_SUBJECT,
        "Hiragana message",
        "Katakana message",
    }

    contents = connection.execute(
        "SELECT subject_norm, sender_norm, body_text, attachment_names "
        "FROM message_contents WHERE body_text LIKE ?",
        ("%cp932%",),
    ).fetchone()
    assert contents is not None
    assert contents[0] == normalize_for_search(SEARCHABLE_SUBJECT)
    assert contents[1] == normalize_for_search("Search Sender <SEARCH@example.test>")
    assert contents[2] == normalize_for_search(searchable_body)
    assert contents[3] == normalize_for_search(regular_attachment)

    plan = parse_query(FULLWIDTH_SEARCH)
    assert plan.match_terms == ('"search"',)
    assert _subjects(
        search,
        FULLWIDTH_SEARCH,
        filters=MessageFilter(account_ids=(account_id,)),
    ) == {SEARCHABLE_SUBJECT}


def _subjects(
    search: SqliteSearchRepository,
    query: str,
    *,
    filters: MessageFilter | None = None,
) -> set[str]:
    return {
        item.subject
        for item in search_messages(
            search,
            query=query,
            filters=filters,
        ).items
    }
