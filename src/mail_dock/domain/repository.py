"""Repository port used only to replace SQLite with an in-memory test double."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any

from mail_dock.domain.messages import StoredEml

type MessageRecord = Mapping[str, Any]
type MessageContents = Mapping[str, str | None]


class BaseMessageRepository(ABC):
    """Small use-case port; it is not a general database abstraction."""

    @abstractmethod
    def upsert_account(self, account: MessageRecord) -> Any:
        ...

    @abstractmethod
    def list_accounts(self) -> Sequence[MessageRecord]:
        ...

    @abstractmethod
    def upsert_folder(self, folder: MessageRecord) -> Any:
        ...

    @abstractmethod
    def list_folders(self, account_id: str) -> Sequence[MessageRecord]:
        ...

    @abstractmethod
    def list_sync_targets(self, account_id: str) -> Sequence[MessageRecord]:
        ...

    @abstractmethod
    def set_sync_target(self, account_id: str, raw_name: str, enabled: bool) -> None:
        ...

    @abstractmethod
    def initialize_sync_cursors(self, folder_id: Any, uidvalidity: int, max_uid: int) -> None:
        ...

    @abstractmethod
    def update_sync_cursors(
        self,
        folder_id: Any,
        *,
        last_seen_uid: int | None = None,
        backfill_next_uid: int | None = None,
        initial_sync_completed: bool | None = None,
    ) -> None:
        ...

    @abstractmethod
    def add_message(self, record: MessageRecord, contents: MessageContents | None = None) -> Any:
        ...

    @abstractmethod
    def exists_source_item_key(self, account_id: str, folder_id: Any, source_item_key: str) -> bool:
        ...

    @abstractmethod
    def find_stored_eml(self, account_id: str, file_hash: str) -> StoredEml | None:
        ...

    @abstractmethod
    def local_uids(self, account_id: str, folder_id: Any, uidvalidity: int) -> set[int]:
        ...

    @abstractmethod
    def find_move_candidates(
        self,
        account_id: str,
        content_key: str,
        file_hash: str | None,
        exclude_folder_id: Any,
    ) -> Sequence[MessageRecord]:
        ...

    @abstractmethod
    def update_remote_state(
        self, message_id: Any, state: str, moved_to_folder_id: Any = None
    ) -> None:
        ...

    @abstractmethod
    def record_failure(
        self,
        account_id: str,
        folder_id: Any,
        uidvalidity: int,
        uid: int,
        error_class: str,
        message: str,
    ) -> None:
        ...

    @abstractmethod
    def pending_failures(
        self, account_id: str, folder_id: Any, uidvalidity: int
    ) -> Sequence[MessageRecord]:
        ...

    @abstractmethod
    def clear_failure(self, account_id: str, folder_id: Any, uidvalidity: int, uid: int) -> None:
        ...

    @abstractmethod
    def list_reparse_targets(
        self, account_id: str | None, only_failed: bool
    ) -> Sequence[MessageRecord]:
        ...

    @abstractmethod
    def update_message_contents(self, message_id: Any, contents: MessageContents) -> None:
        ...

    @abstractmethod
    def begin_batch(self) -> None:
        ...

    @abstractmethod
    def commit_batch(self) -> None:
        ...

    @abstractmethod
    def checkpoint(self) -> None:
        ...