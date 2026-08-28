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
        self.audit_log: list[dict[str, Any]] = []
        self.app_state: dict[str, str] = {}
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
            record.setdefault("highest_modseq", None)
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
            item for item in self.list_folders(account_id) if bool(item.get("is_sync_target", 0))
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
        self.folders[int(folder_id)]["highest_modseq"] = None

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

    def list_flag_refresh_items(
        self,
        account_id: str,
        folder_id: Any,
        uidvalidity: int,
        since_internal_date: str,
    ) -> Sequence[MessageRecord]:
        return [
            {
                "uid": item["uid"],
                "imap_flags": item.get("imap_flags"),
                "flags_seen_at": item.get("flags_seen_at"),
            }
            for item in self.messages.values()
            if item.get("account_id") == account_id
            and item.get("folder_id") == folder_id
            and item.get("uidvalidity") == uidvalidity
            and item.get("uid") is not None
            and item.get("internal_date") is not None
            and str(item["internal_date"]) >= since_internal_date
        ]

    def update_flags(
        self,
        account_id: str,
        folder_id: Any,
        uidvalidity: int,
        uid: int,
        imap_flags: str | None,
        flags_seen_at: str,
    ) -> None:
        for message in self.messages.values():
            if (
                message.get("account_id") == account_id
                and message.get("folder_id") == folder_id
                and message.get("uidvalidity") == uidvalidity
                and message.get("uid") == uid
            ):
                message["imap_flags"] = imap_flags
                message["flags_seen_at"] = flags_seen_at
                return

    def touch_flags_seen_at(
        self,
        account_id: str,
        folder_id: Any,
        uidvalidity: int,
        uids: Sequence[int],
        flags_seen_at: str,
    ) -> None:
        uid_set = set(uids)
        for item in self.messages.values():
            if (
                item.get("account_id") == account_id
                and item.get("folder_id") == folder_id
                and item.get("uidvalidity") == uidvalidity
                and item.get("uid") in uid_set
            ):
                item["flags_seen_at"] = flags_seen_at

    def set_highest_modseq(self, folder_id: Any, value: int | None) -> None:
        folder = self.folders.setdefault(int(folder_id), {"id": int(folder_id)})
        folder["highest_modseq"] = value

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

    def get_message_by_uid(
        self, account_id: str, folder_id: Any, uidvalidity: int, uid: int
    ) -> MessageRecord | None:
        for item in self.messages.values():
            if (
                item.get("account_id") == account_id
                and item.get("folder_id") == folder_id
                and item.get("uidvalidity") == uidvalidity
                and item.get("uid") == uid
            ):
                return item
        return None

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

    def get_message(self, message_id: Any) -> MessageRecord | None:
        item = self.messages.get(int(message_id))
        return None if item is None else self._copy(item)

    def list_stored_messages(self, account_id: str | None = None) -> Sequence[MessageRecord]:
        return [
            self._copy(item)
            for item in self.messages.values()
            if item.get("relative_path") is not None
            and (account_id is None or item.get("account_id") == account_id)
        ]

    def has_message_contents(self, message_id: Any) -> bool:
        return int(message_id) in self.contents

    def update_message_storage(
        self, message_id: Any, relative_path: str | None, file_hash: str | None
    ) -> None:
        item = self.messages[int(message_id)]
        previous_hash = item.get("file_hash")
        if isinstance(previous_hash, str):
            self.stored_eml.pop((str(item["account_id"]), previous_hash), None)
        item["relative_path"] = relative_path
        item["file_hash"] = file_hash
        if isinstance(relative_path, str) and isinstance(file_hash, str):
            self.stored_eml[(str(item["account_id"]), file_hash)] = StoredEml(
                relative_path=relative_path,
                file_hash=file_hash,
                size_bytes=int(item.get("size_bytes", 0)),
            )

    def record_audit(self, entry: MessageRecord) -> None:
        if entry.get("operation") is None:
            raise ValueError("Audit operation is required")
        self.audit_log.append(self._copy(entry))

    def list_audit_log(self, limit: int, offset: int) -> Sequence[MessageRecord]:
        if limit < 0 or offset < 0:
            raise ValueError("limit and offset must be non-negative")
        records = sorted(
            enumerate(self.audit_log),
            key=lambda indexed: (str(indexed[1].get("occurred_at", "")), indexed[0]),
            reverse=True,
        )
        return [self._copy(item) for _, item in records[offset : offset + limit]]

    def set_local_state(self, message_id: Any, state: str, trashed_at: str | None = None) -> None:
        item = self.messages[int(message_id)]
        item["local_state"] = state
        item["trashed_at"] = trashed_at

    def list_trashed(
        self, account_id: str | None = None, older_than: str | None = None
    ) -> Sequence[MessageRecord]:
        items = [
            item
            for item in self.messages.values()
            if item.get("local_state") == "trashed"
            and (account_id is None or item.get("account_id") == account_id)
            and (older_than is None or str(item.get("trashed_at", "")) < older_than)
        ]
        return [
            self._copy(item)
            for item in sorted(
                items,
                key=lambda item: (
                    item.get("trashed_at") is None,
                    str(item.get("trashed_at", "")),
                    item["id"],
                ),
            )
        ]

    def count_path_references(
        self, account_id: str, relative_path: str, exclude_message_id: Any
    ) -> int:
        return sum(
            1
            for message_id, item in self.messages.items()
            if message_id != int(exclude_message_id)
            and item.get("account_id") == account_id
            and item.get("relative_path") == relative_path
            and item.get("local_state") != "purged"
        )

    def delete_message_contents(self, message_id: Any) -> None:
        self.contents.pop(int(message_id), None)

    def get_app_state(self, key: str) -> str | None:
        return self.app_state.get(key)

    def set_app_state(self, key: str, value: str) -> None:
        self.app_state[key] = value

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

    def list_failures_for_review(
        self, account_id: str | None = None, minimum_attempt_count: int = 10
    ) -> Sequence[MessageRecord]:
        if minimum_attempt_count < 0:
            raise ValueError("minimum_attempt_count must be non-negative")
        rows: list[dict[str, Any]] = []
        for failure in self.failures.values():
            if int(failure["attempt_count"]) < minimum_attempt_count:
                continue
            if account_id is not None and failure["account_id"] != account_id:
                continue
            row = self._copy(failure)
            message = next(
                (
                    item
                    for item in self.messages.values()
                    if item.get("account_id") == failure["account_id"]
                    and item.get("folder_id") == failure["folder_id"]
                    and item.get("uidvalidity") == failure["uidvalidity"]
                    and item.get("uid") == failure["uid"]
                ),
                None,
            )
            if message is not None:
                row.update(
                    {
                        "message_id": message.get("id"),
                        "subject": message.get("subject"),
                        "size_bytes": message.get("size_bytes"),
                        "relative_path": message.get("relative_path"),
                        "file_hash": message.get("file_hash"),
                    }
                )
            folder = self.folders.get(int(failure["folder_id"]))
            if folder is not None:
                row["folder_raw_name"] = folder.get("raw_name")
                row["folder_display_name"] = folder.get("display_name")
            rows.append(row)
        return sorted(
            rows,
            key=lambda item: (str(item.get("last_failed_at", "")), int(item.get("uid", 0))),
            reverse=True,
        )

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
