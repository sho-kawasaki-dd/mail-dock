"""Observable in-memory repository used by use-case unit tests."""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from typing import Any

from mail_dock.domain.messages import StoredEml
from mail_dock.domain.repository import BaseMessageRepository, MessageContents, MessageRecord


class InMemoryMessageRepository(BaseMessageRepository):
    """A small stateful fake that mirrors the Phase 1 repository port."""

    def __init__(self) -> None:
        self.accounts: dict[str, dict[str, Any]] = {}
        self.folders: dict[int, dict[str, Any]] = {}
        self.messages: dict[int, dict[str, Any]] = {}
        self.contents: dict[int, dict[str, str | None]] = {}
        self.failures: dict[tuple[str, int, int, int], dict[str, Any]] = {}
        self.cursors: dict[int, dict[str, Any]] = {}
        self.stored_eml: dict[tuple[str, str], StoredEml] = {}
        self.batch_open = False
        self.begin_batch_count = 0
        self.commit_batch_count = 0
        self.checkpoint_count = 0
        self._next_folder_id = 1
        self._next_message_id = 1

    @staticmethod
    def _copy(record: MessageRecord) -> dict[str, Any]:
        return deepcopy(dict(record))

    def upsert_account(self, account: MessageRecord) -> str:
        record = self._copy(account)
        account_id = str(record["id"] if "id" in record else record["account_id"])
        record["id"] = account_id
        self.accounts[account_id] = record
        return account_id

    def list_accounts(self) -> Sequence[MessageRecord]:
        return list(self.accounts.values())

    def upsert_folder(self, folder: MessageRecord) -> int:
        record = self._copy(folder)
        account_id = str(record["account_id"])
        raw_name = str(record["raw_name"])
        existing = next(
            (
                item
                for item in self.folders.values()
                if item["account_id"] == account_id and item["raw_name"] == raw_name
            ),
            None,
        )
        if existing is None:
            folder_id = int(record.get("id", self._next_folder_id))
            self._next_folder_id = max(self._next_folder_id, folder_id + 1)
            record["id"] = folder_id
            record.setdefault("is_sync_target", 0)
            self.folders[folder_id] = record
        else:
            folder_id = int(existing["id"])
            record["id"] = folder_id
            record.setdefault("is_sync_target", existing.get("is_sync_target", 0))
            self.folders[folder_id] = {**existing, **record}
        return folder_id

    def list_folders(self, account_id: str) -> Sequence[MessageRecord]:
        return [item for item in self.folders.values() if item["account_id"] == account_id]

    def list_sync_targets(self, account_id: str) -> Sequence[MessageRecord]:
        return [
            item
            for item in self.list_folders(account_id)
            if bool(item.get("is_sync_target", 0))
        ]

    def set_sync_target(self, account_id: str, raw_name: str, enabled: bool) -> None:
        for item in self.folders.values():
            if item["account_id"] == account_id and item["raw_name"] == raw_name:
                item["is_sync_target"] = int(enabled)
                return
        raise KeyError((account_id, raw_name))

    def initialize_sync_cursors(self, folder_id: Any, uidvalidity: int, max_uid: int) -> None:
        self.cursors[int(folder_id)] = {
            "uidvalidity": uidvalidity,
            "last_seen_uid": max_uid,
            "backfill_next_uid": max_uid,
            "initial_sync_completed": int(max_uid == 0),
        }
        self.folders.setdefault(int(folder_id), {"id": int(folder_id)}).update(
            self.cursors[int(folder_id)]
        )

    def update_sync_cursors(
        self,
        folder_id: Any,
        *,
        last_seen_uid: int | None = None,
        backfill_next_uid: int | None = None,
        initial_sync_completed: bool | None = None,
    ) -> None:
        cursor = self.cursors.setdefault(int(folder_id), {})
        if last_seen_uid is not None:
            cursor["last_seen_uid"] = last_seen_uid
        if backfill_next_uid is not None:
            cursor["backfill_next_uid"] = backfill_next_uid
        if initial_sync_completed is not None:
            cursor["initial_sync_completed"] = int(initial_sync_completed)
        self.folders.setdefault(int(folder_id), {"id": int(folder_id)}).update(cursor)

    def add_message(self, record: MessageRecord, contents: MessageContents | None = None) -> int:
        incoming = self._copy(record)
        key = (
            incoming.get("account_id"),
            incoming.get("folder_id"),
            incoming.get("uidvalidity"),
            incoming.get("uid"),
        )
        existing_id = next(
            (
                message_id
                for message_id, item in self.messages.items()
                if (
                    item.get("account_id"),
                    item.get("folder_id"),
                    item.get("uidvalidity"),
                    item.get("uid"),
                )
                == key
                and incoming.get("uid") is not None
            ),
            None,
        )
        message_id = existing_id or int(incoming.get("id", self._next_message_id))
        self._next_message_id = max(self._next_message_id, message_id + 1)
        incoming["id"] = message_id
        self.messages[message_id] = {**self.messages.get(message_id, {}), **incoming}
        if contents is not None:
            self.contents[message_id] = dict(contents)
        relative_path = incoming.get("relative_path")
        file_hash = incoming.get("file_hash")
        if isinstance(relative_path, str) and isinstance(file_hash, str):
            self.stored_eml[(str(incoming["account_id"]), file_hash)] = StoredEml(
                relative_path=relative_path,
                file_hash=file_hash,
                size_bytes=int(incoming.get("size_bytes", 0)),
            )
        return message_id

    def exists_source_item_key(self, account_id: str, folder_id: Any, source_item_key: str) -> bool:
        return any(
            item.get("account_id") == account_id
            and item.get("folder_id") == folder_id
            and item.get("source_item_key") == source_item_key
            for item in self.messages.values()
        )

    def find_stored_eml(self, account_id: str, file_hash: str) -> StoredEml | None:
        return self.stored_eml.get((account_id, file_hash))

    def local_uids(self, account_id: str, folder_id: Any, uidvalidity: int) -> set[int]:
        return {
            int(item["uid"])
            for item in self.messages.values()
            if item.get("account_id") == account_id
            and item.get("folder_id") == folder_id
            and item.get("uidvalidity") == uidvalidity
            and item.get("uid") is not None
        }

    def find_move_candidates(
        self,
        account_id: str,
        content_key: str,
        file_hash: str | None,
        exclude_folder_id: Any,
    ) -> Sequence[MessageRecord]:
        return [
            item
            for item in self.messages.values()
            if item.get("account_id") == account_id
            and item.get("folder_id") != exclude_folder_id
            and item.get("remote_state", "present") == "present"
            and item.get("content_key") == content_key
            and (file_hash is None or item.get("file_hash") == file_hash)
        ]

    def update_remote_state(
        self, message_id: Any, state: str, moved_to_folder_id: Any = None
    ) -> None:
        item = self.messages[int(message_id)]
        item["remote_state"] = state
        item["moved_to_folder_id"] = moved_to_folder_id

    def record_failure(
        self,
        account_id: str,
        folder_id: Any,
        uidvalidity: int,
        uid: int,
        error_class: str,
        message: str,
    ) -> None:
        key = (account_id, int(folder_id), uidvalidity, uid)
        record = self.failures.get(key, {"attempt_count": 0})
        self.failures[key] = {
            **record,
            "account_id": account_id,
            "folder_id": int(folder_id),
            "uidvalidity": uidvalidity,
            "uid": uid,
            "error_class": error_class,
            "error_message": message,
            "attempt_count": int(record["attempt_count"]) + 1,
        }

    def pending_failures(
        self, account_id: str, folder_id: Any, uidvalidity: int
    ) -> Sequence[MessageRecord]:
        return [
            record
            for record in self.failures.values()
            if record["account_id"] == account_id
            and record["folder_id"] == int(folder_id)
            and record["uidvalidity"] == uidvalidity
            and int(record["attempt_count"]) < 10
        ]

    def clear_failure(self, account_id: str, folder_id: Any, uidvalidity: int, uid: int) -> None:
        self.failures.pop((account_id, int(folder_id), uidvalidity, uid), None)

    def list_reparse_targets(
        self, account_id: str | None, only_failed: bool
    ) -> Sequence[MessageRecord]:
        targets = [
            item
            for item in self.messages.values()
            if (account_id is None or item.get("account_id") == account_id)
            and item.get("relative_path") is not None
        ]
        if only_failed:
            failed_ids = {
                (record["account_id"], record["folder_id"], record["uid"])
                for record in self.failures.values()
                if record["error_class"] == "parse"
            }
            targets = [
                item
                for item in targets
                if (item.get("account_id"), item.get("folder_id"), item.get("uid")) in failed_ids
            ]
        return targets

    def update_message_contents(self, message_id: Any, contents: MessageContents) -> None:
        self.contents[int(message_id)] = dict(contents)

    def begin_batch(self) -> None:
        self.batch_open = True
        self.begin_batch_count += 1

    def commit_batch(self) -> None:
        if not self.batch_open:
            raise RuntimeError("commit_batch called without begin_batch")
        self.batch_open = False
        self.commit_batch_count += 1

    def checkpoint(self) -> None:
        self.checkpoint_count += 1