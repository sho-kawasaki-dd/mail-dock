from __future__ import annotations

import pytest

from tests.support.imap_integration import (
    append_message,
    create_mailbox,
    imap_client,
    make_fetcher,
    service,
    unique_mailbox,
)


@pytest.mark.docker
def test_dovecot_delete_to_special_use_trash_and_expunge() -> None:
    settings = service("dovecot")
    trash_source = unique_mailbox("DeleteTrash")
    expunge_source = unique_mailbox("DeleteExpunge")
    with imap_client(settings) as client:
        create_mailbox(client, trash_source)
        create_mailbox(client, expunge_source)
        append_message(client, trash_source, body="move to trash")
        append_message(client, expunge_source, body="expunge permanently")

    fetcher = make_fetcher(settings)
    try:
        fetcher.connect()
        folders = fetcher.list_folders()
        trash = next(
            folder
            for folder in folders
            if r"\trash" in {item.casefold() for item in folder.special_use}
        )
        trash_raw_name = trash.raw_name
        assert trash.delimiter == "."

        trash_uid = next(iter(fetcher.list_existing_uids(trash_source)))
        fetcher.delete_remote_message(trash_source, trash_uid, mode="trash")
        assert trash_uid not in fetcher.list_existing_uids(trash_source)
        assert fetcher.list_existing_uids(trash_raw_name)

        expunge_uid = next(iter(fetcher.list_existing_uids(expunge_source)))
        fetcher.delete_remote_message(expunge_source, expunge_uid, mode="expunge")
        assert expunge_uid not in fetcher.list_existing_uids(expunge_source)
    finally:
        fetcher.disconnect()
