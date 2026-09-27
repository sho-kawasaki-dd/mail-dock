"""Synchronize selected IMAP folders into durable local mail storage."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from mail_dock.domain.errors import (
    AuthenticationError,
    FetchError,
    OperationCancelledError,
    PermanentError,
    StorageError,
    TransientError,
)
from mail_dock.domain.fetcher import BaseMailFetcher, CancelToken, RemoteMessageRef
from mail_dock.domain.message_identity import imap_source_item_key, remote_source_item_key
from mail_dock.domain.messages import ParsedMessage, StoredEml
from mail_dock.domain.ports import BaseEmlStorage, BaseManifestWriter, BaseMessageParser, JSONValue
from mail_dock.domain.repository import BaseMessageRepository, MessageContents, MessageRecord
from mail_dock.domain.time_format import to_utc_iso8601
from mail_dock.usecases.account_guards import ensure_imap_account
from mail_dock.usecases.retry import with_retry
from mail_dock.usecases.snapshots import folder_snapshot_event

_LOGGER = logging.getLogger(__name__)
_BATCH_MESSAGE_LIMIT = 100
_BATCH_BYTES_LIMIT = 50 * 1024 * 1024
_SQLITE_MAX_INTEGER = 2**63 - 1


@dataclass(frozen=True)
class SyncOptions:
    """Provider-independent options for one account synchronization."""

    max_message_bytes: int = 50 * 1024 * 1024
    flag_refresh_enabled: bool = True
    flag_refresh_window_days: int = 30
    flag_refresh_min_interval_seconds: int = 3600

    def __post_init__(self) -> None:
        if self.max_message_bytes <= 0:
            raise ValueError("max_message_bytes must be positive")
        if not isinstance(self.flag_refresh_enabled, bool):
            raise TypeError("flag_refresh_enabled must be a boolean")
        if self.flag_refresh_window_days <= 0:
            raise ValueError("flag_refresh_window_days must be positive")
        if self.flag_refresh_min_interval_seconds <= 0:
            raise ValueError("flag_refresh_min_interval_seconds must be positive")


@dataclass(frozen=True)
class SyncProgress:
    """Progress measured primarily by bytes transferred from the provider."""

    transferred_bytes: int
    total_bytes_estimate: int
    message_count: int
    current_folder: str
    eta_seconds: float | None


@dataclass(frozen=True)
class SyncResult:
    """Summary of one account synchronization run."""

    fetched_count: int
    transferred_bytes: int
    skipped_count: int
    failed_count: int
    cancelled: bool

    @property
    def message_count(self) -> int:
        """Return the number of successfully handled message references."""

        return self.fetched_count

    @property
    def failure_count(self) -> int:
        """Descriptive alias for ``failed_count``."""

        return self.failed_count


@dataclass
class _MutableStats:
    fetched_count: int = 0
    transferred_bytes: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    estimated_bytes: int = 0
    started_at: float = 0.0


@dataclass
class _PendingItem:
    uid: int
    event: Mapping[str, JSONValue] | None = None
    record: MessageRecord | None = None
    failure_folder_id: Any | None = None
    contents: MessageContents | None = None
    failure: tuple[int, str, str] | None = None
    clear_failure: bool = False
    transferred_bytes: int = 0


@dataclass
class _LocalMessage:
    message_id: Any
    record: MessageRecord


@dataclass(frozen=True)
class _MoveStateEvent:
    """One deletion/move-detection outcome, staged for the durable write order."""

    message_id: Any
    folder_id: Any
    moved_to: Any
    event: Mapping[str, JSONValue]
    record: Mapping[str, Any]
    remove_membership: bool
    merge_candidate: Mapping[str, Any] | None = None


def _now_iso() -> str:
    return to_utc_iso8601(datetime.now(UTC))


def _date_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return to_utc_iso8601(value)


def _valid_sqlite_modseq(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value <= 0 or value > _SQLITE_MAX_INTEGER:
        return None
    return value


def _flag_seen_at_is_expired(
    value: object,
    *,
    now: datetime,
    minimum_interval: timedelta,
) -> bool:
    if not isinstance(value, str) or not value:
        return True
    try:
        seen_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    if seen_at.tzinfo is None:
        seen_at = seen_at.replace(tzinfo=UTC)
    return now - seen_at >= minimum_interval


def _message_contents(parsed: ParsedMessage, *, empty: bool = False) -> MessageContents:
    if empty:
        return {
            "subject_norm": "",
            "sender_norm": "",
            "body_text": "",
            "attachment_names": "",
        }
    attachment_names = "\n".join(
        attachment.filename
        for attachment in parsed.attachments
        if attachment.filename is not None and not attachment.is_inline
    )
    return {
        "subject_norm": parsed.subject,
        "sender_norm": parsed.sender,
        "body_text": parsed.body_text,
        "attachment_names": attachment_names,
    }


def _source_item_key(
    folder_raw_name: str,
    uidvalidity: int,
    uid: int,
    gmail_msgid: str | None = None,
) -> str:
    return remote_source_item_key(folder_raw_name, uidvalidity, uid, gmail_msgid)


def _fetch_event(
    *,
    account_id: str,
    folder_raw_name: str,
    folder_id: Any,
    uidvalidity: int,
    ref: RemoteMessageRef,
    parsed: ParsedMessage,
    stored: StoredEml,
    gmail_msgid: str | None = None,
    gmail_thrid: str | None = None,
    gmail_labels: list[str] | None = None,
) -> Mapping[str, JSONValue]:
    return {
        "event": "fetch",
        "account_id": account_id,
        "folder_id": folder_id,
        "folder_raw_name": folder_raw_name,
        "uid": ref.uid,
        "uidvalidity": uidvalidity,
        "source_item_key": _source_item_key(folder_raw_name, uidvalidity, ref.uid, gmail_msgid),
        "message_id": parsed.message_id or ref.message_id,
        "relative_path": stored.relative_path,
        "file_hash": stored.file_hash,
        "size_bytes": stored.size_bytes,
        "internal_date": _date_iso(ref.internal_date),
        "timestamp": _now_iso(),
        "deduplicated": stored.deduplicated,
        "gmail_msgid": gmail_msgid,
        "gmail_thrid": gmail_thrid,
        "gmail_labels": gmail_labels,
    }


def _skipped_event(
    *,
    account_id: str,
    folder_raw_name: str,
    folder_id: Any,
    uidvalidity: int,
    ref: RemoteMessageRef,
    size_bytes: int,
) -> Mapping[str, JSONValue]:
    return {
        "event": "fetch_skipped",
        "account_id": account_id,
        "folder_id": folder_id,
        "folder_raw_name": folder_raw_name,
        "uid": ref.uid,
        "uidvalidity": uidvalidity,
        "size_bytes": size_bytes,
        "reason": "oversize",
        "timestamp": _now_iso(),
    }


def _membership_snapshot_event(
    repo: BaseMessageRepository,
    account_id: str,
    record: Mapping[str, Any],
    *,
    remove_folder_id: Any | None = None,
    additional_records: Sequence[Mapping[str, Any]] = (),
) -> dict[str, JSONValue]:
    source_item_key = str(record["source_item_key"])
    folders = {
        folder.get("id"): str(folder.get("raw_name", ""))
        for folder in repo.list_folders(account_id)
    }
    moved_names = {folder_id: name for folder_id, name in folders.items()}
    memberships = {
        membership.get("folder_id"): dict(membership)
        for membership in repo.list_message_memberships(account_id, source_item_key)
    }
    if remove_folder_id is not None:
        memberships.pop(remove_folder_id, None)
    else:
        incoming_folder_id = record.get("folder_id")
        memberships[incoming_folder_id] = {
            "folder_id": incoming_folder_id,
            "uid": record.get("uid"),
            "uidvalidity": record.get("uidvalidity"),
            "remote_state": record.get("remote_state", "present"),
            "moved_to_folder_id": record.get("moved_to_folder_id"),
            "imap_flags": record.get("imap_flags"),
            "flags_seen_at": record.get("flags_seen_at"),
            "last_seen_at": record.get("last_seen_at"),
        }
    for incoming_record in additional_records:
        incoming_folder_id = incoming_record.get("folder_id")
        memberships[incoming_folder_id] = {
            "folder_id": incoming_folder_id,
            "uid": incoming_record.get("uid"),
            "uidvalidity": incoming_record.get("uidvalidity"),
            "remote_state": incoming_record.get("remote_state", "present"),
            "moved_to_folder_id": incoming_record.get("moved_to_folder_id"),
            "imap_flags": incoming_record.get("imap_flags"),
            "flags_seen_at": incoming_record.get("flags_seen_at"),
            "last_seen_at": incoming_record.get("last_seen_at"),
        }
    serialized = [
        {
            "folder_raw_name": folders.get(folder_id, membership.get("folder_raw_name", "")),
            "uid": membership.get("uid"),
            "uidvalidity": membership.get("uidvalidity"),
            "remote_state": membership.get("remote_state", "present"),
            "moved_to_folder_raw_name": moved_names.get(membership.get("moved_to_folder_id")),
            "imap_flags": membership.get("imap_flags"),
            "flags_seen_at": membership.get("flags_seen_at"),
            "last_seen_at": membership.get("last_seen_at"),
        }
        for folder_id, membership in sorted(
            memberships.items(), key=lambda item: str(folders.get(item[0], ""))
        )
    ]
    snapshot: dict[str, Any] = {
        "event": "message_membership_snapshot",
        "account_id": account_id,
        "source_item_key": source_item_key,
        "memberships": serialized,
        "timestamp": _now_iso(),
    }
    labels = record.get("gmail_labels")
    if isinstance(labels, str):
        try:
            labels = json.loads(labels)
        except ValueError:
            labels = None
    if isinstance(labels, (list, tuple)) and all(isinstance(label, str) for label in labels):
        snapshot["gmail_labels"] = list(labels)
    return cast(dict[str, JSONValue], snapshot)


def _record_for_message(
    *,
    account_id: str,
    folder_id: Any,
    folder_raw_name: str,
    uidvalidity: int,
    ref: RemoteMessageRef,
    parsed: ParsedMessage,
    stored: StoredEml | None,
    size_bytes: int,
) -> dict[str, Any]:
    file_hash = stored.file_hash if stored is not None else None
    content_key = parsed.content_key or (
        f"sha256:{file_hash[:32]}" if file_hash is not None else f"uid:{uidvalidity}:{ref.uid}"
    )
    return {
        "account_id": account_id,
        "folder_id": folder_id,
        "message_id": parsed.message_id or ref.message_id,
        "content_key": content_key,
        "source_item_key": _source_item_key(folder_raw_name, uidvalidity, ref.uid, ref.gmail_msgid),
        "uid": ref.uid,
        "uidvalidity": uidvalidity,
        "remote_state": "present",
        "moved_to_folder_id": None,
        "local_state": "active",
        "relative_path": stored.relative_path if stored is not None else None,
        "file_hash": file_hash,
        "subject": parsed.subject,
        "sender": parsed.sender,
        "recipient": parsed.recipient,
        "cc": parsed.cc,
        "date_sent": _date_iso(parsed.date_sent),
        "internal_date": _date_iso(ref.internal_date),
        "size_bytes": size_bytes,
        "has_attachment": int(parsed.has_attachment),
        "imap_flags": " ".join(ref.flags),
        "flags_seen_at": _now_iso(),
        "in_reply_to": parsed.in_reply_to,
        "references_ids": parsed.references_ids,
        "thread_key": parsed.thread_key,
        "last_seen_at": _now_iso(),
        "gmail_msgid": ref.gmail_msgid,
        "gmail_thrid": ref.gmail_thrid,
        "gmail_labels": (
            json.dumps(ref.gmail_labels, ensure_ascii=False, separators=(",", ":"))
            if ref.gmail_labels is not None
            else None
        ),
    }


def _record_fetch_failure(
    *,
    uid: int,
    uidvalidity: int,
    error: Exception,
) -> _PendingItem:
    if isinstance(error, AuthenticationError):
        error_class = "auth"
    elif isinstance(error, TransientError):
        error_class = "transient"
    else:
        error_class = "permanent"
    return _PendingItem(
        uid=uid,
        failure=(uidvalidity, error_class, str(error)),
    )


def _get_int(record: Mapping[str, object], key: str, default: int = 0) -> int:
    value = record.get(key)
    return int(value) if isinstance(value, int) else default


def _get_local_message(
    repo: BaseMessageRepository,
    account_id: str,
    folder_id: Any,
    uidvalidity: int,
    uid: int,
) -> _LocalMessage | None:
    record = repo.get_message_by_uid(account_id, folder_id, uidvalidity, uid)
    if record is None or record.get("id") is None:
        return None
    return _LocalMessage(record["id"], record)


def sync_account(
    fetcher: BaseMailFetcher,
    repo: BaseMessageRepository,
    storage: BaseEmlStorage,
    manifest: BaseManifestWriter,
    *,
    account_id: str,
    options: SyncOptions,
    parser: BaseMessageParser,
    cancel: CancelToken | None = None,
    on_progress: Callable[[SyncProgress], None] | None = None,
) -> SyncResult:
    """Synchronize all enabled folders for one account.

    EML placement happens before manifest durability, and manifest durability
    happens before the corresponding database batch. A failed individual
    message is recorded and does not stop the account, except authentication
    failures which abort the run after already completed work is committed.
    """

    ensure_imap_account(repo, account_id)
    token = cancel or CancelToken()
    stats = _MutableStats(started_at=time.monotonic())
    google_account = any(
        account.get("id") == account_id and account.get("oauth_provider") == "google"
        for account in repo.list_accounts()
    )
    targets = [dict(folder) for folder in repo.list_sync_targets(account_id)]
    known_messages: dict[tuple[Any, int, int], _LocalMessage] = {}
    checkpoint_sequence = manifest.last_checkpoint_sequence or 0

    def report(folder_name: str) -> None:
        if on_progress is None:
            return
        elapsed = time.monotonic() - stats.started_at
        rate = stats.transferred_bytes / elapsed if elapsed > 0 else 0.0
        remaining = max(stats.estimated_bytes - stats.transferred_bytes, 0)
        eta = remaining / rate if rate > 0 else None
        on_progress(
            SyncProgress(
                transferred_bytes=stats.transferred_bytes,
                total_bytes_estimate=stats.estimated_bytes,
                message_count=stats.fetched_count,
                current_folder=folder_name,
                eta_seconds=eta,
            )
        )

    def commit_pending(
        pending: list[_PendingItem],
        *,
        cursor_folder_id: Any | None = None,
        last_seen_uid: int | None = None,
        backfill_next_uid: int | None = None,
        initial_sync_completed: bool | None = None,
    ) -> None:
        nonlocal checkpoint_sequence
        if not pending and cursor_folder_id is None:
            return
        snapshot_records: dict[str, list[Mapping[str, Any]]] = {}
        identity_aliases: list[tuple[str, str, str, str]] = []
        for item in pending:
            if item.event is not None:
                manifest.append(item.event)
                if (
                    item.event.get("event") == "fetch"
                    and item.record is not None
                    and item.record.get("source_item_key") is not None
                ):
                    source_item_key = str(item.record["source_item_key"])
                    snapshot_records.setdefault(source_item_key, []).append(item.record)
                    gmail_msgid = item.record.get("gmail_msgid")
                    folder_raw_name = item.event.get("folder_raw_name")
                    uidvalidity = item.record.get("uidvalidity")
                    uid = item.record.get("uid")
                    file_hash = item.record.get("file_hash")
                    if (
                        isinstance(gmail_msgid, str)
                        and isinstance(folder_raw_name, str)
                        and isinstance(uidvalidity, int)
                        and isinstance(uid, int)
                        and isinstance(file_hash, str)
                    ):
                        alias_key = imap_source_item_key(folder_raw_name, uidvalidity, uid)
                        if alias_key != source_item_key:
                            identity_aliases.append(
                                (source_item_key, alias_key, "gmail_msgid", file_hash)
                            )
                            manifest.append(
                                {
                                    "event": "message_identity_linked",
                                    "account_id": account_id,
                                    "canonical_source_item_key": source_item_key,
                                    "alias_source_item_key": alias_key,
                                    "evidence_kind": "gmail_msgid",
                                    "file_hash": file_hash,
                                    "timestamp": _now_iso(),
                                }
                            )
        for records in snapshot_records.values():
            manifest.append(
                _membership_snapshot_event(
                    repo,
                    account_id,
                    records[0],
                    additional_records=records[1:],
                )
            )
        manifest.flush_and_sync()
        repo.begin_batch()
        for item in pending:
            if item.record is not None:
                message_id = repo.add_message(item.record, item.contents)
                stored_record = dict(item.record)
                stored_record["id"] = message_id
                key = (
                    stored_record.get("folder_id"),
                    _get_int(stored_record, "uidvalidity"),
                    _get_int(stored_record, "uid"),
                )
                known_messages[key] = _LocalMessage(message_id, stored_record)
                for canonical_key, alias_key, evidence_kind, _file_hash in identity_aliases:
                    if canonical_key == stored_record.get("source_item_key"):
                        repo.add_message_identity_alias(
                            account_id, alias_key, message_id, evidence_kind
                        )
            if item.failure is not None:
                failure_uidvalidity, error_class, message = item.failure
                repo.record_failure(
                    account_id,
                    item.failure_folder_id,
                    failure_uidvalidity,
                    item.uid,
                    error_class,
                    message,
                )
            elif item.clear_failure and item.record is not None:
                successful_record = item.record
                repo.clear_failure(
                    account_id,
                    successful_record["folder_id"],
                    int(successful_record["uidvalidity"]),
                    item.uid,
                )
        if cursor_folder_id is not None:
            repo.update_sync_cursors(
                cursor_folder_id,
                last_seen_uid=last_seen_uid,
                backfill_next_uid=backfill_next_uid,
                initial_sync_completed=initial_sync_completed,
            )
        repo.commit_batch()
        pending.clear()
        checkpoint_sequence += 1
        manifest.checkpoint(checkpoint_sequence, uuid4().hex)
        if checkpoint_sequence % 10 == 0:
            repo.checkpoint()

    def make_item(
        folder_raw_name: str,
        folder_id: Any,
        uidvalidity: int,
        ref: RemoteMessageRef,
    ) -> _PendingItem:
        size_bytes = ref.size_bytes if ref.size_bytes is not None else 0
        if ref.size_bytes is not None:
            stats.estimated_bytes += ref.size_bytes

        if size_bytes > options.max_message_bytes:
            headers = with_retry(
                lambda: fetcher.download_eml_headers(folder_raw_name, ref.uid), cancel=token
            )
            parsed = parser.parse(headers, ref.internal_date)
            record = _record_for_message(
                account_id=account_id,
                folder_id=folder_id,
                folder_raw_name=folder_raw_name,
                uidvalidity=uidvalidity,
                ref=ref,
                parsed=parsed,
                stored=None,
                size_bytes=size_bytes,
            )
            stats.fetched_count += 1
            stats.skipped_count += 1
            stats.failed_count += 1
            stats.transferred_bytes += len(headers)
            return _PendingItem(
                uid=ref.uid,
                event=_skipped_event(
                    account_id=account_id,
                    folder_raw_name=folder_raw_name,
                    folder_id=folder_id,
                    uidvalidity=uidvalidity,
                    ref=ref,
                    size_bytes=size_bytes,
                ),
                record=record,
                failure=(uidvalidity, "oversize", "message exceeds configured size limit"),
                transferred_bytes=len(headers),
            )

        raw = with_retry(lambda: fetcher.download_eml_bytes(folder_raw_name, ref.uid), cancel=token)
        stats.transferred_bytes += len(raw)
        if ref.size_bytes is None:
            stats.estimated_bytes += len(raw)
        if len(raw) > options.max_message_bytes:
            parsed = parser.parse(raw, ref.internal_date)
            record = _record_for_message(
                account_id=account_id,
                folder_id=folder_id,
                folder_raw_name=folder_raw_name,
                uidvalidity=uidvalidity,
                ref=ref,
                parsed=parsed,
                stored=None,
                size_bytes=len(raw),
            )
            stats.fetched_count += 1
            stats.skipped_count += 1
            stats.failed_count += 1
            return _PendingItem(
                uid=ref.uid,
                event=_skipped_event(
                    account_id=account_id,
                    folder_raw_name=folder_raw_name,
                    folder_id=folder_id,
                    uidvalidity=uidvalidity,
                    ref=ref,
                    size_bytes=len(raw),
                ),
                record=record,
                failure=(uidvalidity, "oversize", "message exceeds configured size limit"),
                transferred_bytes=len(raw),
            )

        file_hash = hashlib.sha256(raw).hexdigest()
        existing = repo.find_stored_eml(account_id, file_hash)
        stored = storage.reuse(existing.relative_path, file_hash) if existing is not None else None
        if stored is None:
            stored = storage.save(account_id, ref.internal_date, raw)
        parsed = parser.parse(raw, ref.internal_date)
        record = _record_for_message(
            account_id=account_id,
            folder_id=folder_id,
            folder_raw_name=folder_raw_name,
            uidvalidity=uidvalidity,
            ref=ref,
            parsed=parsed,
            stored=stored,
            size_bytes=len(raw),
        )
        parse_failed = parsed.parse_error is not None
        if parse_failed:
            stats.failed_count += 1
        stats.fetched_count += 1
        return _PendingItem(
            uid=ref.uid,
            event=_fetch_event(
                account_id=account_id,
                folder_raw_name=folder_raw_name,
                folder_id=folder_id,
                uidvalidity=uidvalidity,
                ref=ref,
                parsed=parsed,
                stored=stored,
                gmail_msgid=ref.gmail_msgid,
                gmail_thrid=ref.gmail_thrid,
                gmail_labels=(list(ref.gmail_labels) if ref.gmail_labels is not None else None),
            ),
            record=record,
            contents=_message_contents(parsed, empty=parse_failed),
            failure=(uidvalidity, "parse", parsed.parse_error or "message parsing failed")
            if parse_failed
            else None,
            clear_failure=not parse_failed,
            transferred_bytes=len(raw),
        )

    def process_ref(
        folder_raw_name: str,
        folder_id: Any,
        uidvalidity: int,
        ref: RemoteMessageRef,
        pending: list[_PendingItem],
    ) -> None:
        token.raise_if_cancelled()
        try:
            item = make_item(folder_raw_name, folder_id, uidvalidity, ref)
            if item.failure is not None:
                item.failure_folder_id = folder_id
        except AuthenticationError:
            raise
        except FetchError as error:
            stats.failed_count += 1
            item = _record_fetch_failure(uid=ref.uid, uidvalidity=uidvalidity, error=error)
            item.failure_folder_id = folder_id
        except StorageError as error:
            stats.failed_count += 1
            item = _record_fetch_failure(uid=ref.uid, uidvalidity=uidvalidity, error=error)
            item.failure_folder_id = folder_id
        pending.append(item)

    def retry_pending_failures(
        folder_raw_name: str,
        folder_id: Any,
        uidvalidity: int,
        attempted: set[int],
        pending: list[_PendingItem],
    ) -> None:
        for failure in repo.pending_failures(account_id, folder_id, uidvalidity):
            uid_value = failure.get("uid")
            if not isinstance(uid_value, int):
                continue
            uid = uid_value
            ref = next(
                iter(
                    fetcher.iter_message_refs(
                        folder_raw_name,
                        min_uid=uid,
                        max_uid=uid,
                        descending=True,
                        cancel=token,
                    )
                ),
                None,
            )
            if ref is None:
                continue
            if (
                failure.get("error_class") == "oversize"
                and ref.size_bytes is not None
                and ref.size_bytes > options.max_message_bytes
            ):
                attempted.add(uid)
                continue
            attempted.add(uid)
            process_ref(folder_raw_name, folder_id, uidvalidity, ref, pending)
            if (
                len(pending) >= _BATCH_MESSAGE_LIMIT
                or sum(pending_item.transferred_bytes for pending_item in pending)
                >= _BATCH_BYTES_LIMIT
            ):
                commit_pending(pending)

    def process_range(
        folder_raw_name: str,
        folder_id: Any,
        uidvalidity: int,
        *,
        min_uid: int,
        max_uid: int,
        pending: list[_PendingItem],
        attempted: set[int],
        history: bool,
    ) -> tuple[bool, int | None]:
        minimum_seen: int | None = None
        iterator = fetcher.iter_message_refs(
            folder_raw_name,
            min_uid=min_uid,
            max_uid=max_uid,
            descending=True,
            cancel=token,
        )
        for ref in iterator:
            if ref.uid in attempted:
                continue
            attempted.add(ref.uid)
            process_ref(folder_raw_name, folder_id, uidvalidity, ref, pending)
            minimum_seen = ref.uid if minimum_seen is None else min(minimum_seen, ref.uid)
            batch_is_full = (
                len(pending) >= _BATCH_MESSAGE_LIMIT
                or sum(pending_item.transferred_bytes for pending_item in pending)
                >= _BATCH_BYTES_LIMIT
            )
            if batch_is_full:
                if history:
                    next_cursor = max(minimum_seen - 1, 0)
                    commit_pending(
                        pending,
                        cursor_folder_id=folder_id,
                        backfill_next_uid=next_cursor,
                        initial_sync_completed=next_cursor == 0,
                    )
                    minimum_seen = None
                else:
                    commit_pending(pending)
            report(folder_raw_name)
        return minimum_seen is not None, minimum_seen

    def refresh_flags(folder: dict[str, object], folder_raw_name: str) -> None:
        if not options.flag_refresh_enabled or not bool(folder.get("initial_sync_completed", 0)):
            return

        folder_id = folder["id"]
        uidvalidity = _get_int(folder, "uidvalidity")
        now = datetime.now(UTC)
        seen_at = to_utc_iso8601(now)
        since = to_utc_iso8601(now - timedelta(days=options.flag_refresh_window_days))
        items = repo.list_flag_refresh_items(account_id, folder_id, uidvalidity, since)
        local_flags: dict[int, str | None] = {}
        local_labels: dict[int, tuple[str, ...]] = {}
        refresh_items: dict[int, Mapping[str, Any]] = {}
        expired_uids: set[int] = set()
        minimum_interval = timedelta(seconds=options.flag_refresh_min_interval_seconds)
        for item in items:
            uid = item.get("uid")
            if not isinstance(uid, int) or isinstance(uid, bool):
                continue
            local_flags[uid] = cast(str | None, item.get("imap_flags"))
            refresh_items[uid] = item
            try:
                saved_labels = json.loads(str(item.get("gmail_labels") or "[]"))
            except (TypeError, ValueError):
                saved_labels = []
            if isinstance(saved_labels, list) and all(
                isinstance(label, str) for label in saved_labels
            ):
                local_labels[uid] = tuple(saved_labels)
            else:
                local_labels[uid] = ()
            if _flag_seen_at_is_expired(
                item.get("flags_seen_at"),
                now=now,
                minimum_interval=minimum_interval,
            ):
                expired_uids.add(uid)
        capabilities = getattr(fetcher, "capabilities", ())
        condstore_supported = "CONDSTORE" in {
            str(capability).upper() for capability in capabilities
        }
        current_modseq = _valid_sqlite_modseq(fetcher.get_highest_modseq())
        raw_saved_modseq = folder.get("highest_modseq")
        saved_modseq = _valid_sqlite_modseq(raw_saved_modseq)
        use_condstore = condstore_supported and current_modseq is not None
        saved_modseq_is_ahead = False
        if saved_modseq is not None and current_modseq is not None:
            saved_modseq_is_ahead = saved_modseq > current_modseq
        if not expired_uids:
            needs_modseq_reset = raw_saved_modseq is not None and (
                not use_condstore or saved_modseq is None or saved_modseq_is_ahead
            )
            if needs_modseq_reset:
                repo.begin_batch()
                repo.set_highest_modseq(folder_id, None)
                repo.commit_batch()
            return
        baseline = use_condstore and (saved_modseq is None or saved_modseq_is_ahead)

        pending_changes: list[tuple[int, str | None]] = []
        pending_touches: list[int] = []
        pending_label_updates: dict[int, tuple[str, ...]] = {}

        def flush_flag_updates() -> None:
            if not pending_changes and not pending_touches and not pending_label_updates:
                return
            if google_account:
                folders_by_id = {item.get("id"): item for item in repo.list_folders(account_id)}
                snapshots_by_source_key: dict[str, list[Mapping[str, Any]]] = {}
                for uid in pending_label_updates:
                    labels = pending_label_updates[uid]
                    item = refresh_items[uid]
                    folder_record = folders_by_id.get(folder_id, {})
                    source_item_key = item.get("source_item_key")
                    folder_name = folder_record.get("raw_name")
                    if not isinstance(source_item_key, str) or not isinstance(folder_name, str):
                        raise StorageError("Gmail flag refresh lacks membership identity")
                    membership_record = dict(item)
                    membership_record.update(
                        {
                            "account_id": account_id,
                            "folder_id": folder_id,
                            "folder_raw_name": folder_name,
                            "imap_flags": next(
                                (
                                    flags
                                    for changed_uid, flags in pending_changes
                                    if changed_uid == uid
                                ),
                                item.get("imap_flags"),
                            ),
                            "last_seen_at": seen_at,
                            "gmail_labels": list(labels),
                        }
                    )
                    snapshots_by_source_key.setdefault(source_item_key, []).append(
                        membership_record
                    )
                for records in snapshots_by_source_key.values():
                    manifest.append(
                        _membership_snapshot_event(
                            repo,
                            account_id,
                            records[0],
                            additional_records=records[1:],
                        )
                    )
                if pending_label_updates:
                    manifest.flush_and_sync()
            repo.begin_batch()
            try:
                for uid, flags in pending_changes:
                    repo.update_flags(
                        account_id,
                        folder_id,
                        uidvalidity,
                        uid,
                        flags,
                        seen_at,
                    )
                if pending_touches:
                    repo.touch_flags_seen_at(
                        account_id,
                        folder_id,
                        uidvalidity,
                        tuple(pending_touches),
                        seen_at,
                    )
                for uid, labels in pending_label_updates.items():
                    message_id = refresh_items[uid].get("message_id")
                    if message_id is not None:
                        repo.update_gmail_labels(
                            message_id,
                            json.dumps(list(labels), ensure_ascii=False, separators=(",", ":")),
                        )
                repo.commit_batch()
            finally:
                pending_changes.clear()
                pending_touches.clear()
                pending_label_updates.clear()

        def queue_update(
            uid: int, flags: str | None, gmail_labels: tuple[str, ...] | None = None
        ) -> None:
            pending_changes.append((uid, flags))
            if (
                google_account
                and gmail_labels is not None
                and tuple(sorted(gmail_labels)) != tuple(sorted(local_labels.get(uid, ())))
            ):
                pending_label_updates[uid] = gmail_labels
            if len(pending_changes) + len(pending_touches) + len(pending_label_updates) >= 500:
                flush_flag_updates()

        def queue_touch(uid: int) -> None:
            pending_touches.append(uid)
            if len(pending_changes) + len(pending_touches) + len(pending_label_updates) >= 500:
                flush_flag_updates()

        response_uids: set[int] = set()
        if baseline:
            iterator = fetcher.iter_flags(
                folder_raw_name,
                sorted(local_flags),
                cancel=token,
            )
            for ref in iterator:
                token.raise_if_cancelled()
                if ref.uid not in local_flags:
                    continue
                response_uids.add(ref.uid)
                queue_update(ref.uid, " ".join(ref.flags), ref.gmail_labels)
            token.raise_if_cancelled()
            flush_flag_updates()
            if current_modseq is not None:
                repo.begin_batch()
                try:
                    repo.set_highest_modseq(folder_id, current_modseq)
                    repo.commit_batch()
                finally:
                    pending_changes.clear()
                    pending_touches.clear()
            return

        if use_condstore and saved_modseq is not None and current_modseq is not None:
            iterator = fetcher.iter_flags_since(
                folder_raw_name,
                saved_modseq,
                cancel=token,
            )
            for ref in iterator:
                token.raise_if_cancelled()
                if ref.uid not in local_flags:
                    continue
                response_uids.add(ref.uid)
                queue_update(ref.uid, " ".join(ref.flags), ref.gmail_labels)
            token.raise_if_cancelled()
            for uid in sorted(expired_uids - response_uids):
                queue_touch(uid)
            flush_flag_updates()
            if current_modseq is not None:
                repo.begin_batch()
                try:
                    repo.set_highest_modseq(folder_id, current_modseq)
                    repo.commit_batch()
                finally:
                    pending_changes.clear()
                    pending_touches.clear()
            return

        iterator = fetcher.iter_flags(
            folder_raw_name,
            sorted(expired_uids),
            cancel=token,
        )
        for ref in iterator:
            token.raise_if_cancelled()
            if ref.uid not in local_flags:
                continue
            response_uids.add(ref.uid)
            remote_flags = " ".join(ref.flags)
            labels_changed = (
                google_account
                and ref.gmail_labels is not None
                and tuple(sorted(ref.gmail_labels)) != tuple(sorted(local_labels.get(ref.uid, ())))
            )
            if local_flags[ref.uid] == remote_flags and not labels_changed:
                queue_touch(ref.uid)
            else:
                queue_update(ref.uid, remote_flags, ref.gmail_labels)
        token.raise_if_cancelled()
        flush_flag_updates()
        if not use_condstore and saved_modseq is not None:
            repo.begin_batch()
            try:
                repo.set_highest_modseq(folder_id, None)
                repo.commit_batch()
            finally:
                pending_changes.clear()
                pending_touches.clear()

    def sync_folder(folder: dict[str, object]) -> bool:
        folder_raw_name = str(folder["raw_name"])
        folder_id = folder["id"]
        current_uidvalidity = fetcher.select_folder(folder_raw_name)
        old_uidvalidity = folder.get("uidvalidity")
        folder["uidvalidity"] = current_uidvalidity
        last_seen_uid = _get_int(folder, "last_seen_uid")
        backfill_next_uid = folder.get("backfill_next_uid")
        initial_completed = bool(folder.get("initial_sync_completed", 0))
        needs_initialization = old_uidvalidity != current_uidvalidity or old_uidvalidity is None
        if needs_initialization:
            max_uid = fetcher.get_max_uid(folder_raw_name)
            repo.begin_batch()
            repo.initialize_sync_cursors(folder_id, current_uidvalidity, max_uid)
            repo.commit_batch()
            # Record the newly known uidvalidity so reindex can recover it; the
            # folders table update above has no manifest counterpart otherwise.
            manifest.append(folder_snapshot_event(account_id, folder))
            manifest.flush_and_sync()
            last_seen_uid = max_uid
            backfill_next_uid = max_uid
            initial_completed = max_uid == 0
        elif backfill_next_uid is None:
            backfill_next_uid = fetcher.get_max_uid(folder_raw_name)
            repo.begin_batch()
            repo.update_sync_cursors(
                folder_id,
                backfill_next_uid=int(backfill_next_uid),
                initial_sync_completed=initial_completed,
            )
            repo.commit_batch()

        pending: list[_PendingItem] = []
        attempted: set[int] = set()
        try:
            retry_pending_failures(
                folder_raw_name, folder_id, current_uidvalidity, attempted, pending
            )
            new_max_uid = fetcher.get_max_uid(folder_raw_name)
            if new_max_uid > last_seen_uid:
                process_range(
                    folder_raw_name,
                    folder_id,
                    current_uidvalidity,
                    min_uid=last_seen_uid + 1,
                    max_uid=new_max_uid,
                    pending=pending,
                    attempted=attempted,
                    history=False,
                )
                commit_pending(pending, cursor_folder_id=folder_id, last_seen_uid=new_max_uid)
                last_seen_uid = new_max_uid

            history_limit = int(backfill_next_uid) if isinstance(backfill_next_uid, int) else 0
            if not initial_completed and history_limit > 0:
                _, minimum_seen = process_range(
                    folder_raw_name,
                    folder_id,
                    current_uidvalidity,
                    min_uid=1,
                    max_uid=history_limit,
                    pending=pending,
                    attempted=attempted,
                    history=True,
                )
                if pending:
                    next_cursor = max(minimum_seen - 1, 0) if minimum_seen is not None else 0
                    commit_pending(
                        pending,
                        cursor_folder_id=folder_id,
                        backfill_next_uid=next_cursor,
                        initial_sync_completed=next_cursor == 0,
                    )
                elif minimum_seen is None:
                    repo.begin_batch()
                    repo.update_sync_cursors(
                        folder_id, backfill_next_uid=0, initial_sync_completed=True
                    )
                    repo.commit_batch()
                backfill_next_uid = 0 if minimum_seen is None else max(minimum_seen - 1, 0)
                initial_completed = backfill_next_uid == 0
            folder["initial_sync_completed"] = int(initial_completed)

            catchup_max_uid = fetcher.get_max_uid(folder_raw_name)
            if catchup_max_uid > last_seen_uid:
                process_range(
                    folder_raw_name,
                    folder_id,
                    current_uidvalidity,
                    min_uid=last_seen_uid + 1,
                    max_uid=catchup_max_uid,
                    pending=pending,
                    attempted=attempted,
                    history=False,
                )
                commit_pending(pending, cursor_folder_id=folder_id, last_seen_uid=catchup_max_uid)
            elif pending:
                commit_pending(pending)
            try:
                refresh_flags(folder, folder_raw_name)
            except AuthenticationError:
                raise
            except OperationCancelledError:
                raise
            except FetchError as error:
                _LOGGER.warning(
                    "Could not refresh IMAP flags: folder=%s error=%s",
                    folder_raw_name,
                    error,
                )
            return False
        except OperationCancelledError:
            if pending:
                commit_pending(pending)
            return True
        except AuthenticationError:
            if pending:
                commit_pending(pending)
            raise

    scanned_folder_ids: set[Any] = set()
    for folder in targets:
        try:
            if sync_folder(folder):
                return SyncResult(
                    stats.fetched_count,
                    stats.transferred_bytes,
                    stats.skipped_count,
                    stats.failed_count,
                    True,
                )
        except AuthenticationError:
            raise
        except FetchError as error:
            _LOGGER.error("Folder synchronization failed: account=%s error=%s", account_id, error)
        else:
            scanned_folder_ids.add(folder["id"])

    # A duplicate is only folded automatically when both the folder that lost
    # the UID and the folder holding the matching content were both fully
    # scanned this cycle; anything less certain stays "unknown" (D-30).
    supports_move_merge = not google_account and repo.supports_message_folders()
    folder_raw_names_by_id = {folder["id"]: str(folder["raw_name"]) for folder in targets}

    # Deletion and move detection deliberately never removes the EML itself.
    for folder in targets:
        folder_raw_name = str(folder["raw_name"])
        folder_id = folder["id"]
        uidvalidity = _get_int(folder, "uidvalidity")
        try:
            remote_uids = fetcher.list_existing_uids(folder_raw_name)
        except FetchError as error:
            _LOGGER.warning(
                "Could not inspect remote UIDs: folder=%s error=%s", folder_raw_name, error
            )
            continue
        missing_uids = repo.local_uids(account_id, folder_id, uidvalidity) - remote_uids
        state_events: list[_MoveStateEvent] = []
        for uid in sorted(missing_uids):
            local = known_messages.get((folder_id, uidvalidity, uid)) or _get_local_message(
                repo, account_id, folder_id, uidvalidity, uid
            )
            if local is None:
                continue
            content_key = local.record.get("content_key")
            file_hash = local.record.get("file_hash")
            candidates: Sequence[MessageRecord] = ()
            if isinstance(content_key, str) and isinstance(file_hash, str):
                candidates = repo.find_move_candidates(
                    account_id, content_key, file_hash, folder_id
                )
                candidates = tuple(
                    candidate
                    for candidate in candidates
                    if candidate.get("content_key") == content_key
                    and candidate.get("file_hash") == file_hash
                )
            merge_candidate: Mapping[str, Any] | None = None
            can_merge = (
                supports_move_merge
                and isinstance(file_hash, str)
                and folder_id in scanned_folder_ids
            )
            if can_merge:
                # Require the candidate to be a fetch freshly observed this
                # cycle, not content that already existed at that location
                # before now (which would signal a COPY, not a MOVE).
                fully_scanned_candidates = tuple(
                    candidate
                    for candidate in candidates
                    if candidate.get("folder_id") in scanned_folder_ids
                    and (
                        candidate.get("folder_id"),
                        candidate.get("uidvalidity"),
                        candidate.get("uid"),
                    )
                    in known_messages
                )
                if len(fully_scanned_candidates) == 1:
                    merge_candidate = fully_scanned_candidates[0]
            moved_to_raw_name: str | None = None
            if merge_candidate is not None:
                moved_to = merge_candidate.get("folder_id")
                moved_to_raw_name = folder_raw_names_by_id.get(moved_to)
                event_name = "moved"
            elif google_account or (len(candidates) == 0 and isinstance(file_hash, str)):
                moved_to = None
                event_name = "delete_detected"
            else:
                moved_to = None
                event_name = "remote_state_unknown"
            source_item_key = str(
                local.record.get("source_item_key")
                or _source_item_key(folder_raw_name, uidvalidity, uid)
            )
            remove_membership = google_account
            event: dict[str, JSONValue] = {
                "event": event_name,
                "account_id": account_id,
                "folder_raw_name": folder_raw_name,
                "uid": uid,
                "uidvalidity": uidvalidity,
                "source_item_key": source_item_key,
                "message_id": cast(JSONValue, local.record.get("message_id")),
                "content_key": cast(JSONValue, content_key),
                "file_hash": cast(JSONValue, file_hash),
                "timestamp": _now_iso(),
            }
            if moved_to_raw_name is not None:
                event["moved_to_folder_raw_name"] = moved_to_raw_name
            state_events.append(
                _MoveStateEvent(
                    message_id=local.message_id,
                    folder_id=folder_id,
                    moved_to=moved_to,
                    event=event,
                    record=local.record,
                    remove_membership=remove_membership,
                    merge_candidate=merge_candidate,
                )
            )
        if state_events:
            for item in state_events:
                manifest.append(item.event)
            for item in state_events:
                if item.remove_membership:
                    manifest.append(
                        _membership_snapshot_event(
                            repo,
                            account_id,
                            item.record,
                            remove_folder_id=item.folder_id,
                        )
                    )
                elif item.merge_candidate is not None:
                    canonical_key = str(item.record["source_item_key"])
                    alias_key = str(item.merge_candidate.get("source_item_key"))
                    merge_file_hash = item.merge_candidate.get("file_hash")
                    manifest.append(
                        {
                            "event": "message_identity_linked",
                            "account_id": account_id,
                            "canonical_source_item_key": canonical_key,
                            "alias_source_item_key": alias_key,
                            "evidence_kind": "file_hash_move",
                            "file_hash": merge_file_hash,
                            "timestamp": _now_iso(),
                        }
                    )
                    manifest.append(
                        _membership_snapshot_event(
                            repo,
                            account_id,
                            {
                                **item.record,
                                "remote_state": "moved",
                                "moved_to_folder_id": item.moved_to,
                            },
                            additional_records=(item.merge_candidate,),
                        )
                    )
            manifest.flush_and_sync()
            repo.begin_batch()
            for item in state_events:
                if item.remove_membership:
                    state_source_key = item.event.get("source_item_key")
                    if not isinstance(state_source_key, str) or not state_source_key:
                        raise StorageError("Gmail membership removal has no canonical source key")
                    remaining = [
                        membership
                        for membership in repo.list_message_memberships(
                            account_id, state_source_key
                        )
                        if membership.get("folder_id") != item.folder_id
                    ]
                    repo.replace_message_memberships(item.message_id, remaining)
                    continue
                if item.merge_candidate is not None:
                    duplicate_id = item.merge_candidate.get("id")
                    repo.merge_duplicate_message(account_id, item.message_id, duplicate_id)
                    repo.update_remote_state(
                        item.message_id, "moved", item.moved_to, item.folder_id
                    )
                    continue
                event_name = str(item.event["event"])
                state = {
                    "delete_detected": "deleted",
                    "remote_state_unknown": "unknown",
                    "moved": "moved",
                }[event_name]
                repo.update_remote_state(item.message_id, state, item.moved_to, item.folder_id)
            repo.commit_batch()

    return SyncResult(
        stats.fetched_count,
        stats.transferred_bytes,
        stats.skipped_count,
        stats.failed_count,
        False,
    )


def force_fetch_message(
    fetcher: BaseMailFetcher,
    repo: BaseMessageRepository,
    storage: BaseEmlStorage,
    manifest: BaseManifestWriter,
    *,
    account_id: str,
    folder_raw_name: str,
    folder_id: Any,
    uidvalidity: int,
    uid: int,
    parser: BaseMessageParser,
    cancel: CancelToken | None = None,
) -> SyncResult:
    """Fetch one message while deliberately bypassing the normal size limit.

    This operation is only exposed through the review UI. It keeps the normal
    durable ordering and clears the matching failure after the message has
    been committed successfully.
    """

    token = cancel or CancelToken()
    token.raise_if_cancelled()
    current_uidvalidity = fetcher.select_folder(folder_raw_name)
    if current_uidvalidity != uidvalidity:
        raise PermanentError("message UIDVALIDITY has changed; run synchronization again")
    ref = next(
        iter(
            fetcher.iter_message_refs(
                folder_raw_name,
                min_uid=uid,
                max_uid=uid,
                descending=True,
                cancel=token,
            )
        ),
        None,
    )
    if ref is None:
        raise PermanentError("message no longer exists on the server")
    raw = with_retry(lambda: fetcher.download_eml_bytes(folder_raw_name, uid), cancel=token)
    token.raise_if_cancelled()
    file_hash = hashlib.sha256(raw).hexdigest()
    existing = repo.find_stored_eml(account_id, file_hash)
    stored = storage.reuse(existing.relative_path, file_hash) if existing is not None else None
    if stored is None:
        stored = storage.save(account_id, ref.internal_date, raw)
    parsed = parser.parse(raw, ref.internal_date)
    record = _record_for_message(
        account_id=account_id,
        folder_id=folder_id,
        folder_raw_name=folder_raw_name,
        uidvalidity=uidvalidity,
        ref=ref,
        parsed=parsed,
        stored=stored,
        size_bytes=len(raw),
    )
    parse_failed = parsed.parse_error is not None
    event = _fetch_event(
        account_id=account_id,
        folder_raw_name=folder_raw_name,
        folder_id=folder_id,
        uidvalidity=uidvalidity,
        ref=ref,
        parsed=parsed,
        stored=stored,
    )
    manifest.append(event)
    manifest.flush_and_sync()
    repo.begin_batch()
    try:
        repo.add_message(record, _message_contents(parsed, empty=parse_failed))
        if parse_failed:
            repo.record_failure(
                account_id,
                folder_id,
                uidvalidity,
                uid,
                "parse",
                parsed.parse_error or "message parsing failed",
            )
        else:
            repo.clear_failure(account_id, folder_id, uidvalidity, uid)
        repo.commit_batch()
    except Exception:
        raise
    checkpoint_sequence = manifest.last_checkpoint_sequence or 0
    manifest.checkpoint(checkpoint_sequence + 1, uuid4().hex)
    return SyncResult(1, len(raw), 0, int(parse_failed), False)
