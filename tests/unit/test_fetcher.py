from collections.abc import Iterator
from datetime import UTC, datetime
from threading import Event

import pytest

from mail_dock.domain.errors import OperationCancelledError
from mail_dock.domain.fetcher import (
    BaseMailFetcher,
    CancelToken,
    RemoteFolder,
    RemoteMessageRef,
)


class MinimalFetcher(BaseMailFetcher):
    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def list_folders(self) -> list[RemoteFolder]:
        return []

    def select_folder(self, raw_name: str) -> int:
        return 1

    def iter_message_refs(
        self,
        raw_name: str,
        *,
        min_uid: int = 1,
        max_uid: int | None = None,
        descending: bool = True,
        cancel: CancelToken | None = None,
    ) -> Iterator[RemoteMessageRef]:
        return iter(())

    def get_max_uid(self, raw_name: str) -> int:
        return 0

    def list_existing_uids(self, raw_name: str) -> set[int]:
        return set()

    def download_eml_bytes(self, raw_name: str, uid: int) -> bytes:
        return b""

    def download_eml_headers(self, raw_name: str, uid: int) -> bytes:
        return b""

    def delete_remote_message(self, raw_name: str, uid: int, *, mode: str = "trash") -> None:
        pass


def test_cancel_token_uses_injected_event() -> None:
    event = Event()
    token = CancelToken(event)

    assert token.event is event
    assert not token.is_cancelled

    event.set()

    assert token.is_cancelled
    with pytest.raises(OperationCancelledError):
        token.raise_if_cancelled()


def test_remote_value_objects_are_frozen() -> None:
    folder = RemoteFolder("INBOX", "受信トレイ", special_use=frozenset({"\\Inbox"}))
    message = RemoteMessageRef(
        uid=3,
        internal_date=datetime(2026, 7, 30, tzinfo=UTC),
        flags=("\\Seen",),
    )

    with pytest.raises(AttributeError):
        folder.raw_name = "other"  # type: ignore[misc]  # intentionally tests frozen dataclass
    with pytest.raises(AttributeError):
        message.uid = 4  # type: ignore[misc]  # intentionally tests frozen dataclass


def test_fetcher_context_manager_connects_and_disconnects() -> None:
    fetcher = MinimalFetcher()

    with fetcher as active:
        assert active is fetcher
