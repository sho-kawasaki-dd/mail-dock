"""Rebuild the metadata cache from durable manifests and stored EML files."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from mail_dock.domain.errors import OperationCancelledError, StorageError
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.messages import ParsedMessage
from mail_dock.domain.ports import BaseEmlStorage, BaseManifestReader, JSONValue
from mail_dock.domain.repository import BaseMessageRepository, MessageContents, MessageRecord
from mail_dock.infrastructure.parsing.eml_parser import parse_eml

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReindexProgress:
    """Progress for the EML parsing portion of a reindex operation."""

    processed_count: int
    total_count: int
    relative_path: str


@dataclass(frozen=True)
class ReindexResult:
    """Summary of a metadata-cache rebuild."""

    account_count: int
    folder_count: int
    message_count: int
    contents_count: int
    purged_count: int
    skipped_count: int
    warnings: tuple[str, ...]
    cancelled: bool


@dataclass(frozen=True)
class _FetchRecord:
    event: Mapping[str, JSONValue]
    key: tuple[str, str, int, int]


@dataclass(frozen=True)
class _MessageState:
    remote_state: str
    moved_to_folder_raw_name: str | None = None
    local_state: str = "active"


def _text(event: Mapping[str, JSONValue], field: str) -> str | None:
    value = event.get(field)
    return value if isinstance(value, str) and value else None


def _integer(event: Mapping[str, JSONValue], field: str) -> int | None:
    value = event.get(field)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _internal_date(event: Mapping[str, JSONValue]) -> datetime | None:
    value = event.get("internal_date")
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _message_contents(parsed: ParsedMessage) -> MessageContents:
    return {
        "subject_norm": parsed.subject,
        "sender_norm": parsed.sender,
        "body_text": parsed.body_text,
        "attachment_names": "\n".join(
            attachment.filename
            for attachment in parsed.attachments
            if attachment.filename is not None and not attachment.is_inline
        ),
    }


def _event_key(event: Mapping[str, JSONValue]) -> tuple[str, str, int, int] | None:
    account_id = _text(event, "account_id")
    folder_raw_name = _text(event, "folder_raw_name")
    uid = _integer(event, "uid")
    uidvalidity = _integer(event, "uidvalidity")
    if account_id is None or folder_raw_name is None or uid is None or uidvalidity is None:
        return None
    return account_id, folder_raw_name, uidvalidity, uid


def _source_item_key(event: Mapping[str, JSONValue], key: tuple[str, str, int, int]) -> str:
    source_item_key = _text(event, "source_item_key")
    return source_item_key if source_item_key is not None else f"{key[2]}:{key[3]}"


def _account_record(event: Mapping[str, JSONValue]) -> MessageRecord | None:
    account_id = _text(event, "account_id")
    provider_type = _text(event, "provider_type")
    host = _text(event, "host")
    username = _text(event, "username")
    port = _integer(event, "port")
    if (
        account_id is None
        or provider_type is None
        or host is None
        or username is None
        or port is None
    ):
        return None
    return {
        "id": account_id,
        "provider_type": provider_type,
        "display_name": event.get("display_name"),
        "host": host,
        "port": port,
        "username": username,
        "is_enabled": int(bool(event.get("is_enabled", True))),
    }


def _folder_record(event: Mapping[str, JSONValue]) -> MessageRecord | None:
    account_id = _text(event, "account_id")
    raw_name = _text(event, "folder_raw_name")
    display_name = _text(event, "display_name")
    if account_id is None or raw_name is None or display_name is None:
        return None
    uidvalidity = event.get("uidvalidity")
    return {
        "account_id": account_id,
        "raw_name": raw_name,
        "display_name": display_name,
        "uidvalidity": uidvalidity if isinstance(uidvalidity, int) else None,
        # A rebuilt cache must never silently opt a folder into synchronization.
        "is_sync_target": 0,
    }


def _audit_entry(
    event: Mapping[str, JSONValue],
    operation: str,
    related_event: Mapping[str, JSONValue] | None = None,
) -> MessageRecord:
    def value(field: str) -> JSONValue:
        event_value = event.get(field)
        if event_value is not None:
            return event_value
        return related_event.get(field) if related_event is not None else None

    return {
        "occurred_at": event.get("timestamp", datetime.now(UTC).isoformat()),
        "operation": operation,
        "account_id": value("account_id"),
        "message_id": value("message_id"),
        "subject": value("subject"),
        "size_bytes": value("size_bytes"),
        "detail": f"reconstructed from {event.get('event', 'manifest')} event",
    }


def _state_for_event(
    event: Mapping[str, JSONValue],
    states: dict[tuple[str, str, int, int], _MessageState],
    key: tuple[str, str, int, int],
) -> None:
    event_name = event.get("event")
    if event_name == "delete_detected":
        states[key] = _MessageState("deleted")
    elif event_name == "remote_state_unknown":
        states[key] = _MessageState("unknown")
    elif event_name == "moved":
        destination = _text(event, "moved_to_folder_raw_name")
        if destination is not None:
            states[key] = _MessageState("moved", destination)
    elif event_name == "remote_delete_completed":
        states[key] = _MessageState("deleted")
    elif event_name in {"remote_delete_uncertain", "remote_delete_intent"}:
        states[key] = _MessageState("uncertain")


def _purge_key(event: Mapping[str, JSONValue]) -> tuple[str, str, str, str] | None:
    account_id = _text(event, "account_id")
    source_item_key = _text(event, "source_item_key")
    relative_path = _text(event, "relative_path")
    file_hash = _text(event, "file_hash")
    if None in (account_id, source_item_key, relative_path, file_hash):
        return None
    return cast(tuple[str, str, str, str], (account_id, source_item_key, relative_path, file_hash))


def reindex(
    repo: BaseMessageRepository,
    storage: BaseEmlStorage,
    manifest_reader: BaseManifestReader,
    *,
    cancel: CancelToken | None = None,
    on_progress: Callable[[ReindexProgress], None] | None = None,
) -> ReindexResult:
    """Rebuild repository rows from one account's durable manifest.

    The caller supplies a repository connected to the replacement database.
    This function only prepares and writes repository records; creating,
    validating, and atomically replacing the database belongs to infrastructure.
    """

    token = cancel or CancelToken()
    account_events: dict[str, Mapping[str, JSONValue]] = {}
    folder_events: dict[tuple[str, str], Mapping[str, JSONValue]] = {}
    fetches: dict[tuple[str, str, int, int], _FetchRecord] = {}
    states: dict[tuple[str, str, int, int], _MessageState] = {}
    purge_events: dict[tuple[str, str, str, str], Mapping[str, JSONValue]] = {}
    completed_purges: set[tuple[str, str, str, str]] = set()
    audit_events: list[tuple[Mapping[str, JSONValue], str, Mapping[str, JSONValue] | None]] = []
    parsed_metadata: dict[tuple[str, str], Mapping[str, JSONValue]] = {}

    try:
        for event in manifest_reader.read_all_events():
            token.raise_if_cancelled()
            event_name = event.get("event")
            if event_name == "account_snapshot":
                account_id = _text(event, "account_id")
                if account_id is not None:
                    account_events[account_id] = event
            elif event_name == "folder_snapshot":
                account_id = _text(event, "account_id")
                raw_name = _text(event, "folder_raw_name")
                if account_id is not None and raw_name is not None:
                    folder_events[(account_id, raw_name)] = event
            elif event_name == "fetch":
                key = _event_key(event)
                if key is not None:
                    fetches[key] = _FetchRecord(event, key)
            elif event_name in {
                "delete_detected",
                "remote_state_unknown",
                "moved",
                "remote_delete_intent",
                "remote_delete_completed",
                "remote_delete_uncertain",
            }:
                event_key = _event_key(event)
                if event_key is not None:
                    _state_for_event(event, states, event_key)
                    related_fetch_record = fetches.get(event_key)
                    related_fetch_event = (
                        related_fetch_record.event if related_fetch_record is not None else None
                    )
                else:
                    related_fetch_event = None
                if event_name in {"delete_detected", "moved"}:
                    audit_events.append((event, str(event_name), related_fetch_event))
                elif event_name == "remote_delete_completed":
                    audit_events.append((event, "remote_delete", related_fetch_event))
            elif event_name == "purge_intent":
                purge_key = _purge_key(event)
                if purge_key is not None:
                    purge_events[purge_key] = event
            elif event_name == "purged":
                purge_key = _purge_key(event)
                if purge_key is not None:
                    completed_purges.add(purge_key)
                    related_fetch = next(
                        (
                            fetch.event
                            for fetch in fetches.values()
                            if fetch.event.get("account_id") == event.get("account_id")
                            and _source_item_key(event, fetch.key) == event.get("source_item_key")
                        ),
                        None,
                    )
                    audit_events.append((event, "local_purge", related_fetch))
    except OperationCancelledError:
        return ReindexResult(0, 0, 0, 0, 0, 0, (), True)

    warnings: list[str] = []
    for account_id, event in account_events.items():
        if _account_record(event) is None:
            warnings.append(f"invalid account snapshot: {account_id}")
    accounts = {
        account_id: record
        for account_id, event in account_events.items()
        if (record := _account_record(event)) is not None
    }
    folders = {
        folder_key: record
        for folder_key, event in folder_events.items()
        if (record := _folder_record(event)) is not None and folder_key[0] in accounts
    }
    for fetch in fetches.values():
        account_id, folder_raw_name, _, _ = fetch.key
        if account_id not in accounts:
            warnings.append(f"fetch has no account snapshot: {account_id}")
        if (account_id, folder_raw_name) not in folders:
            warnings.append(f"fetch has no folder snapshot: {account_id}/{folder_raw_name}")

    if not folders and fetches:
        warnings.append("no valid folder snapshots were available")

    prepared_messages: list[
        tuple[dict[str, Any], MessageContents | None, tuple[str, str], str | None]
    ] = []
    contents_count = 0
    purged_count = 0
    skipped_count = 0
    try:
        token.raise_if_cancelled()
        valid_fetches = [
            fetch for fetch in fetches.values() if (fetch.key[0], fetch.key[1]) in folders
        ]
        total_count = len(valid_fetches)
        for processed_count, fetch in enumerate(valid_fetches, start=1):
            token.raise_if_cancelled()
            event = fetch.event
            relative_path = _text(event, "relative_path")
            expected_hash = _text(event, "file_hash")
            account_id, folder_raw_name, uidvalidity, uid = fetch.key
            if relative_path is None or expected_hash is None:
                skipped_count += 1
                warnings.append(f"fetch has no stored EML: {account_id}:{uid}")
                continue
            source_item_key = _source_item_key(event, fetch.key)
            completed_purge_key = (account_id, source_item_key, relative_path, expected_hash)
            is_purged = completed_purge_key in completed_purges
            try:
                raw = storage.read_verified(relative_path, expected_hash)
            except (FileNotFoundError, StorageError) as error:
                if not is_purged:
                    skipped_count += 1
                    warnings.append(f"could not verify EML: {relative_path}")
                    _LOGGER.warning(
                        "Skipping EML during reindex: path=%s error=%s", relative_path, error
                    )
                    continue
                # A completed purge intentionally has no EML left to parse.
                raw = None

            parsed = parse_eml(raw, _internal_date(event)) if raw is not None else ParsedMessage()
            state = states.get(fetch.key, _MessageState("present"))
            event_size = event.get("size_bytes")
            size_bytes = event_size if isinstance(event_size, int) else len(raw or b"")
            parsed_metadata[(account_id, source_item_key)] = {
                "message_id": parsed.message_id,
                "subject": parsed.subject or None,
                "size_bytes": size_bytes,
            }
            message: dict[str, Any] = {
                "account_id": account_id,
                "message_id": parsed.message_id or _text(event, "message_id"),
                "content_key": parsed.content_key or f"sha256:{expected_hash[:32]}",
                "source_item_key": source_item_key,
                "uid": uid,
                "uidvalidity": uidvalidity,
                "remote_state": state.remote_state,
                "moved_to_folder_id": None,
                "local_state": "active",
                "relative_path": relative_path,
                "file_hash": expected_hash,
                "subject": parsed.subject,
                "sender": parsed.sender,
                "recipient": parsed.recipient,
                "cc": parsed.cc,
                "date_sent": parsed.date_sent.isoformat() if parsed.date_sent else None,
                "internal_date": event.get("internal_date"),
                "size_bytes": size_bytes,
                "has_attachment": int(parsed.has_attachment),
                "in_reply_to": parsed.in_reply_to,
                "references_ids": parsed.references_ids,
                "thread_key": parsed.thread_key,
            }
            destination = state.moved_to_folder_raw_name
            contents: MessageContents | None = None
            if parsed.parse_error is not None:
                warnings.append(f"EML parse failed: {relative_path}")

            if is_purged:
                message["local_state"] = "purged"
                message["relative_path"] = None
                contents = None
                purged_count += 1
            elif parsed.parse_error is None:
                contents = _message_contents(parsed)
                contents_count += 1
            prepared_messages.append(
                (message, contents, (account_id, folder_raw_name), destination)
            )
            if on_progress is not None:
                on_progress(ReindexProgress(processed_count, total_count, relative_path))

        token.raise_if_cancelled()
        folder_ids: dict[tuple[str, str], Any] = {}
        repo.begin_batch()
        for account in accounts.values():
            repo.upsert_account(account)
        for folder_key, folder in folders.items():
            folder_ids[folder_key] = repo.upsert_folder(folder)
        for message, contents, folder_key, destination in prepared_messages:
            message["folder_id"] = folder_ids[folder_key]
            if destination is not None:
                destination_key = (message["account_id"], destination)
                if destination_key not in folder_ids:
                    folder_ids[destination_key] = repo.upsert_folder(
                        {
                            "account_id": message["account_id"],
                            "raw_name": destination,
                            "display_name": destination,
                            "is_sync_target": 0,
                        }
                    )
                message["moved_to_folder_id"] = folder_ids[destination_key]
            repo.add_message(message, contents)
        repo.commit_batch()
    except OperationCancelledError:
        return ReindexResult(0, 0, 0, 0, 0, 0, tuple(warnings), True)

    for event, operation, related_event in audit_events:
        audit_related = dict(related_event or {})
        account_id = _text(event, "account_id")
        audit_source_item_key = _text(event, "source_item_key")
        event_key = _event_key(event)
        if audit_source_item_key is None and event_key is not None:
            audit_source_item_key = f"{event_key[2]}:{event_key[3]}"
        if account_id is not None and audit_source_item_key is not None:
            for field, value in parsed_metadata.get(
                (account_id, audit_source_item_key), {}
            ).items():
                if value is not None:
                    audit_related[field] = value
        repo.record_audit(_audit_entry(event, operation, audit_related))
    for purge_key, event in purge_events.items():
        del event
        if purge_key not in completed_purges:
            warnings.append(f"incomplete purge intent: {purge_key[1]}")

    return ReindexResult(
        len(accounts),
        len(folders),
        len(prepared_messages),
        contents_count,
        purged_count,
        skipped_count,
        tuple(warnings),
        False,
    )


__all__ = ["ReindexProgress", "ReindexResult", "reindex"]
