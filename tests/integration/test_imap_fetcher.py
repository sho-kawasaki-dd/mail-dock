from __future__ import annotations

import pytest

from mail_dock.infrastructure.fetchers.imap_common import encode_modified_utf7
from tests.support.imap_integration import (
    append_message,
    create_mailbox,
    imap_client,
    make_fetcher,
    service,
    unique_mailbox,
)


@pytest.mark.docker
@pytest.mark.parametrize("service_name", ["greenmail", "dovecot"])
def test_fetcher_connects_lists_folders_and_peek_downloads_without_seen(
    service_name: str,
) -> None:
    settings = service(service_name)
    japanese_mailbox = "受信トレイ/請求書" if service_name == "greenmail" else "受信トレイ.請求書"
    mailbox = unique_mailbox("Fetcher")
    with imap_client(settings) as client:
        if service_name == "greenmail":
            create_mailbox(client, encode_modified_utf7(japanese_mailbox))
        create_mailbox(client, mailbox)
        append_message(client, mailbox, body="peek must not mark this read")

    fetcher = make_fetcher(settings)
    try:
        fetcher.connect()
        folders = fetcher.list_folders()
        folder = next(item for item in folders if item.display_name == japanese_mailbox)
        assert folder.raw_name.startswith("&") or service_name == "dovecot"
        assert folder.delimiter in {"/", "."}

        uidvalidity = fetcher.select_folder(mailbox)
        refs = list(fetcher.iter_message_refs(mailbox, descending=True))
        assert len(refs) == 1
        ref = refs[0]
        assert ref.uid > 0
        assert ref.size_bytes is not None and ref.size_bytes > 0
        assert ref.internal_date is not None
        assert "\\Seen" not in ref.flags
        assert fetcher.list_existing_uids(mailbox) == {ref.uid}
        downloaded = fetcher.download_eml_bytes(mailbox, ref.uid)
        assert b"peek must not mark this read" in downloaded
        assert len(downloaded) > 0
        assert uidvalidity > 0
    finally:
        fetcher.disconnect()

    with imap_client(settings) as client:
        status, data = client.select(mailbox)
        assert status == "OK", data
        status, data = client.uid("FETCH", str(ref.uid), "(FLAGS)")
        assert status == "OK", data
        assert b"\\Seen" not in b" ".join(
            item if isinstance(item, bytes) else str(item).encode() for item in data or []
        )
