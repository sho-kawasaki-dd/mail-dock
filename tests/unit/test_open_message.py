from __future__ import annotations

from unittest.mock import Mock

import pytest

from mail_dock.domain.errors import StorageError
from mail_dock.domain.messages import RenderedMessage
from mail_dock.domain.ports import BaseEmlStorage, BaseMessageRenderer
from mail_dock.domain.search import BaseSearchRepository, MessageDetail
from mail_dock.usecases.open_message import OpenedMessage, open_message


def _detail(
    *, local_state: str = "active", relative_path: str | None = "eml/message.eml"
) -> MessageDetail:
    return MessageDetail(
        id=1,
        account_id="account-a",
        folder_id=1,
        folder_raw_name="INBOX",
        folder_display_name="Inbox",
        subject="Subject",
        sender="sender@example.com",
        date_sent=None,
        internal_date=None,
        size_bytes=4,
        has_attachment=False,
        remote_state="present",
        local_state=local_state,
        thread_key=None,
        imap_flags=None,
        moved_to_folder_display_name=None,
        failure_class=None,
        flags_seen_at=None,
        recipient="recipient@example.com",
        cc="",
        message_id="<message@example.com>",
        in_reply_to=None,
        references_ids=None,
        relative_path=relative_path,
        file_hash="a" * 64,
    )


def _ports(
    detail: MessageDetail,
) -> tuple[Mock, Mock, Mock]:
    search_repo = Mock(spec=BaseSearchRepository)
    search_repo.get_message.return_value = detail
    storage = Mock(spec=BaseEmlStorage)
    renderer = Mock(spec=BaseMessageRenderer)
    renderer.render.return_value = RenderedMessage(None, "body", ())
    return search_repo, storage, renderer


def test_open_message_verifies_eml_before_rendering() -> None:
    detail = _detail()
    search_repo, storage, renderer = _ports(detail)
    storage.read_verified.return_value = b"verified eml"

    result = open_message(search_repo, storage, renderer, message_id=detail.id)

    assert isinstance(result, OpenedMessage)
    assert result.detail == detail
    storage.read_verified.assert_called_once_with(detail.relative_path, detail.file_hash)
    renderer.render.assert_called_once_with(b"verified eml")


@pytest.mark.parametrize(
    ("local_state", "relative_path"),
    [("purged", "eml/message.eml"), ("active", None)],
)
def test_open_message_rejects_unavailable_eml_without_reading(
    local_state: str, relative_path: str | None
) -> None:
    search_repo, storage, renderer = _ports(
        _detail(local_state=local_state, relative_path=relative_path)
    )

    with pytest.raises(StorageError):
        open_message(search_repo, storage, renderer, message_id=1)

    storage.read_verified.assert_not_called()
    renderer.render.assert_not_called()


def test_open_message_propagates_storage_hash_failure() -> None:
    detail = _detail()
    search_repo, storage, renderer = _ports(detail)
    storage.read_verified.side_effect = StorageError("hash mismatch")

    with pytest.raises(StorageError, match="hash mismatch"):
        open_message(search_repo, storage, renderer, message_id=detail.id)

    renderer.render.assert_not_called()
