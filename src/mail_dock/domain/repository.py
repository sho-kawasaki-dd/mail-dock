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
    def upsert_account(self, account: MessageRecord) -> Any: ...

    @abstractmethod
    def list_accounts(self) -> Sequence[MessageRecord]: ...

    @abstractmethod
    def upsert_folder(self, folder: MessageRecord) -> Any: ...

    @abstractmethod
    def list_folders(self, account_id: str) -> Sequence[MessageRecord]: ...

    @abstractmethod
    def list_sync_targets(self, account_id: str) -> Sequence[MessageRecord]: ...

    @abstractmethod
    def set_sync_target(self, account_id: str, raw_name: str, enabled: bool) -> None: ...

    @abstractmethod
    def initialize_sync_cursors(self, folder_id: Any, uidvalidity: int, max_uid: int) -> None: ...

    @abstractmethod
    def update_sync_cursors(
        self,
        folder_id: Any,
        *,
        last_seen_uid: int | None = None,
        backfill_next_uid: int | None = None,
        initial_sync_completed: bool | None = None,
    ) -> None: ...

    @abstractmethod
    def list_flag_refresh_items(
        self,
        account_id: str,
        folder_id: Any,
        uidvalidity: int,
        since_internal_date: str,
    ) -> Sequence[MessageRecord]: ...

    @abstractmethod
    def update_flags(
        self,
        account_id: str,
        folder_id: Any,
        uidvalidity: int,
        uid: int,
        imap_flags: str | None,
        flags_seen_at: str,
    ) -> None: ...

    @abstractmethod
    def touch_flags_seen_at(
        self,
        account_id: str,
        folder_id: Any,
        uidvalidity: int,
        uids: Sequence[int],
        flags_seen_at: str,
    ) -> None: ...

    @abstractmethod
    def set_highest_modseq(self, folder_id: Any, value: int | None) -> None: ...

    @abstractmethod
    def add_message(
        self, record: MessageRecord, contents: MessageContents | None = None
    ) -> Any: ...

    @abstractmethod
    def exists_source_item_key(
        self, account_id: str, folder_id: Any, source_item_key: str
    ) -> bool: ...

    @abstractmethod
    def find_stored_eml(self, account_id: str, file_hash: str) -> StoredEml | None: ...

    @abstractmethod
    def local_uids(self, account_id: str, folder_id: Any, uidvalidity: int) -> set[int]: ...

    @abstractmethod
    def find_move_candidates(
        self,
        account_id: str,
        content_key: str,
        file_hash: str | None,
        exclude_folder_id: Any,
    ) -> Sequence[MessageRecord]: ...

    @abstractmethod
    def update_remote_state(
        self, message_id: Any, state: str, moved_to_folder_id: Any = None
    ) -> None: ...

    @abstractmethod
    def get_message(self, message_id: Any) -> MessageRecord | None: ...

    @abstractmethod
    def list_stored_messages(self, account_id: str | None = None) -> Sequence[MessageRecord]: ...

    @abstractmethod
    def has_message_contents(self, message_id: Any) -> bool: ...

    @abstractmethod
    def update_message_storage(
        self, message_id: Any, relative_path: str | None, file_hash: str | None
    ) -> None: ...

    @abstractmethod
    def record_audit(self, entry: MessageRecord) -> None: ...

    @abstractmethod
    def list_audit_log(self, limit: int, offset: int) -> Sequence[MessageRecord]: ...

    @abstractmethod
    def set_local_state(
        self, message_id: Any, state: str, trashed_at: str | None = None
    ) -> None: ...

    @abstractmethod
    def list_trashed(
        self, account_id: str | None = None, older_than: str | None = None
    ) -> Sequence[MessageRecord]: ...

    @abstractmethod
    def count_path_references(
        self, account_id: str, relative_path: str, exclude_message_id: Any
    ) -> int: ...

    def count_generation_path_references(
        self,
        account_id: str,
        import_uuid: str,
        relative_path: str,
        exclude_message_id: Any,
    ) -> int:
        """Count references within one PST generation."""

        del import_uuid
        return self.count_path_references(account_id, relative_path, exclude_message_id)

    @abstractmethod
    def delete_message_contents(self, message_id: Any) -> None: ...

    @abstractmethod
    def get_app_state(self, key: str) -> str | None: ...

    @abstractmethod
    def set_app_state(self, key: str, value: str) -> None: ...

    @abstractmethod
    def record_failure(
        self,
        account_id: str,
        folder_id: Any,
        uidvalidity: int,
        uid: int,
        error_class: str,
        message: str,
    ) -> None: ...

    @abstractmethod
    def pending_failures(
        self, account_id: str, folder_id: Any, uidvalidity: int
    ) -> Sequence[MessageRecord]: ...

    @abstractmethod
    def list_failures_for_review(
        self, account_id: str | None = None, minimum_attempt_count: int = 10
    ) -> Sequence[MessageRecord]: ...

    @abstractmethod
    def clear_failure(
        self, account_id: str, folder_id: Any, uidvalidity: int, uid: int
    ) -> None: ...

    @abstractmethod
    def list_reparse_targets(
        self, account_id: str | None, only_failed: bool
    ) -> Sequence[MessageRecord]: ...

    @abstractmethod
    def update_message_contents(self, message_id: Any, contents: MessageContents) -> None: ...

    @abstractmethod
    def begin_batch(self) -> None: ...

    @abstractmethod
    def commit_batch(self) -> None: ...

    @abstractmethod
    def checkpoint(self) -> None: ...


class BasePstImportRepository(ABC):
    """Repository port for PST import jobs and their staged items."""

    @abstractmethod
    def record_audit(self, entry: MessageRecord) -> None: ...

    @abstractmethod
    def get_import(self, import_id: int) -> MessageRecord | None: ...

    def list_imports(self) -> Sequence[MessageRecord]:
        """Return PST generations for presentation and recovery views."""

        return ()

    @abstractmethod
    def get_message(self, message_id: int) -> MessageRecord | None: ...

    def get_import_uuid_for_message(self, message_id: int) -> str | None:
        """Return the PST generation containing a message, when applicable."""

        del message_id
        return None

    @abstractmethod
    def list_folders(self, account_id: str) -> Sequence[MessageRecord]: ...

    @abstractmethod
    def upsert_folder(self, folder: MessageRecord) -> Any: ...

    @abstractmethod
    def create_import(self, record: MessageRecord) -> int: ...

    @abstractmethod
    def update_import_status(
        self,
        import_id: int,
        status: str,
        *,
        total_files: int | None = None,
        ingested_count: int | None = None,
        failed_count: int | None = None,
        staging_path: str | None = None,
        finished_at: str | None = None,
        error_message: str | None = None,
    ) -> None: ...

    @abstractmethod
    def find_active_by_source_sha256(self, source_sha256: str) -> MessageRecord | None: ...

    @abstractmethod
    def find_incomplete_by_source_sha256(self, source_sha256: str) -> Sequence[MessageRecord]: ...

    @abstractmethod
    def upsert_import_item(self, record: MessageRecord) -> None: ...

    @abstractmethod
    def add_message(
        self, record: MessageRecord, contents: MessageContents | None = None
    ) -> Any: ...

    @abstractmethod
    def begin_batch(self) -> None: ...

    @abstractmethod
    def commit_batch(self) -> None: ...

    @abstractmethod
    def rollback_batch(self) -> None: ...

    @abstractmethod
    def list_incomplete_items(self, import_id: int) -> Sequence[MessageRecord]: ...

    @abstractmethod
    def list_items(self, import_id: int) -> Sequence[MessageRecord]: ...

    @abstractmethod
    def activate_generation(self, import_id: int, replaces_id: int) -> None: ...

    @abstractmethod
    def rollback_generation_switch(self, import_id: int, replaces_id: int) -> None: ...

    @abstractmethod
    def restore_generation(self, import_id: int) -> None: ...
