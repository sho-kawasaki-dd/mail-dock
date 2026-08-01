"""Open a stored message for display after verifying its EML payload."""

from __future__ import annotations

from dataclasses import dataclass

from mail_dock.domain.errors import StorageError
from mail_dock.domain.messages import RenderedMessage
from mail_dock.domain.ports import BaseEmlStorage, BaseMessageRenderer
from mail_dock.domain.search import BaseSearchRepository, MessageDetail


@dataclass(frozen=True)
class OpenedMessage:
    """A message detail together with its rendered, verified EML content."""

    detail: MessageDetail
    rendered: RenderedMessage


def open_message(
    search_repo: BaseSearchRepository,
    storage: BaseEmlStorage,
    renderer: BaseMessageRenderer,
    *,
    message_id: int,
) -> OpenedMessage:
    """Load and render one message after verifying its stored EML.

    A purged message, or a message without complete storage metadata, cannot
    be opened. These checks happen before touching the storage adapter so the
    caller never attempts to read a missing local artifact.
    """
    detail = search_repo.get_message(message_id)
    if detail is None:
        raise StorageError(f"Message does not exist: {message_id}")
    if detail.local_state == "purged":
        raise StorageError("Message EML is unavailable because it was purged")
    if detail.relative_path is None:
        raise StorageError("Message has no stored EML path")
    if detail.file_hash is None:
        raise StorageError("Message has no stored EML hash")

    raw = storage.read_verified(detail.relative_path, detail.file_hash)
    return OpenedMessage(detail=detail, rendered=renderer.render(raw))
