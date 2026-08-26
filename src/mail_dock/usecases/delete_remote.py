"""Safety-first use cases for deleting messages from an IMAP server."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from mail_dock.domain.errors import (
    FetchError,
    PermanentError,
    StorageDetachedError,
    StorageError,
    TransientError,
)
from mail_dock.domain.fetcher import BaseMailFetcher
from mail_dock.domain.ports import BaseEmlStorage, BaseManifestReader, BaseManifestWriter, JSONValue
from mail_dock.domain.repository import BaseMessageRepository, MessageRecord

_LOGGER = logging.getLogger(__name__)
DEFAULT_DELETE_BATCH_LIMIT = 1000


class RemoteDeleteGate(Protocol):
    """Minimal storage-state contract required by remote deletion."""

    def is_remote_delete_allowed(self) -> bool: ...


@dataclass(frozen=True)
class DeleteCandidate:
    """A message that passed all local safety checks."""

    message_id: Any
    account_id: str
    folder_raw_name: str
    uid: int
    uidvalidity: int
    subject: str
    date_sent: str | None
    internal_date: str | None
    size_bytes: int
    relative_path: str
    file_hash: str
    message_id_header: str | None = None

    @property
    def date(self) -> str | None:
        return self.date_sent or self.internal_date


@dataclass(frozen=True)
class DeleteExclusion:
    """A selected message omitted from a delete plan and why."""

    message_id: Any
    reason: str
    subject: str = ""
    size_bytes: int = 0


@dataclass(frozen=True)
class DeleteDryRunResult:
    """Reviewable remote-delete plan produced without changing the server."""

    candidates: tuple[DeleteCandidate, ...] = ()
    exclusions: tuple[DeleteExclusion, ...] = ()
    total_size_bytes: int = 0

    @property
    def items(self) -> tuple[DeleteCandidate, ...]:
        return self.candidates

    @property
    def included(self) -> tuple[DeleteCandidate, ...]:
        return self.candidates

    @property
    def excluded(self) -> tuple[DeleteExclusion, ...]:
        return self.exclusions

    @property
    def candidate_count(self) -> int:
        return len(self.candidates)

    @property
    def excluded_count(self) -> int:
        return len(self.exclusions)


@dataclass(frozen=True)
class DeleteResult:
    """Outcome of a best-effort remote deletion run."""

    completed_ids: tuple[Any, ...] = ()
    uncertain_ids: tuple[Any, ...] = ()
    skipped_ids: tuple[Any, ...] = ()
    errors: tuple[tuple[Any, str], ...] = ()
    total_size_bytes: int = 0

    @property
    def deleted_ids(self) -> tuple[Any, ...]:
        return self.completed_ids

    @property
    def completed_count(self) -> int:
        return len(self.completed_ids)

    @property
    def uncertain_count(self) -> int:
        return len(self.uncertain_ids)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped_ids)


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _folder_names(
    repo: BaseMessageRepository, account_id: str
) -> dict[Any, tuple[str, int | None]]:
    names: dict[Any, tuple[str, int | None]] = {}
    for folder in repo.list_folders(account_id):
        folder_id = folder.get("id")
        raw_name = folder.get("raw_name")
        if folder_id is not None and isinstance(raw_name, str) and raw_name:
            uidvalidity = folder.get("uidvalidity")
            names[folder_id] = (
                raw_name,
                uidvalidity
                if isinstance(uidvalidity, int) and not isinstance(uidvalidity, bool)
                else None,
            )
    return names


def _candidate_from_record(
    repo: BaseMessageRepository,
    storage: BaseEmlStorage,
    record: MessageRecord,
) -> tuple[DeleteCandidate | None, str | None]:
    message_id = record.get("id")
    subject = record.get("subject")
    subject_text = subject if isinstance(subject, str) else ""
    size_bytes = record.get("size_bytes")
    if message_id is None:
        return None, "message_id_missing"
    if record.get("remote_state") in {"deleted", "uncertain"}:
        return None, "remote_state_not_deletable"

    account_id = record.get("account_id")
    relative_path = record.get("relative_path")
    file_hash = record.get("file_hash")
    uid = record.get("uid")
    uidvalidity = record.get("uidvalidity")
    if not isinstance(account_id, str) or not account_id:
        return None, "account_id_missing"
    if not isinstance(relative_path, str) or not relative_path:
        return None, "eml_missing"
    if not isinstance(file_hash, str) or not file_hash:
        return None, "file_hash_missing"
    if not isinstance(uid, int) or isinstance(uid, bool):
        return None, "uid_missing"
    if not isinstance(uidvalidity, int) or isinstance(uidvalidity, bool):
        return None, "uidvalidity_missing"
    if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes < 0:
        return None, "size_missing"

    folder_raw_name = record.get("folder_raw_name")
    folder_uidvalidity: int | None = None
    if not isinstance(folder_raw_name, str) or not folder_raw_name:
        folder = _folder_names(repo, account_id).get(record.get("folder_id"))
        if folder is None:
            return None, "folder_missing"
        folder_raw_name, folder_uidvalidity = folder
    if folder_uidvalidity is not None and folder_uidvalidity != uidvalidity:
        return None, "uidvalidity_mismatch"

    try:
        storage.read_verified(relative_path, file_hash)
    except StorageDetachedError:
        raise
    except FileNotFoundError:
        return None, "eml_missing"
    except StorageError as error:
        _LOGGER.info("Excluding message %s after EML verification failed", message_id)
        reason = "hash_mismatch" if "hash" in str(error).casefold() else "eml_unreadable"
        return None, reason

    if not repo.has_message_contents(message_id):
        return None, "message_contents_missing"

    return (
        DeleteCandidate(
            message_id=message_id,
            account_id=account_id,
            folder_raw_name=folder_raw_name,
            uid=uid,
            uidvalidity=uidvalidity,
            subject=subject_text,
            date_sent=record.get("date_sent") if isinstance(record.get("date_sent"), str) else None,
            internal_date=(
                record.get("internal_date")
                if isinstance(record.get("internal_date"), str)
                else None
            ),
            size_bytes=size_bytes,
            relative_path=relative_path,
            file_hash=file_hash,
            message_id_header=(
                record.get("message_id") if isinstance(record.get("message_id"), str) else None
            ),
        ),
        None,
    )


def dry_run(
    repo: BaseMessageRepository,
    storage: BaseEmlStorage,
    *,
    message_ids: Iterable[Any],
    storage_state: RemoteDeleteGate,
) -> DeleteDryRunResult:
    """Build a deletion plan after verifying every local prerequisite."""

    if not storage_state.is_remote_delete_allowed():
        raise StorageDetachedError("Remote deletion requires attached storage")

    candidates: list[DeleteCandidate] = []
    exclusions: list[DeleteExclusion] = []
    for message_id in message_ids:
        record = repo.get_message(message_id)
        if record is None:
            exclusions.append(DeleteExclusion(message_id, "message_not_found"))
            continue
        candidate, reason = _candidate_from_record(repo, storage, record)
        if candidate is None:
            exclusions.append(
                DeleteExclusion(
                    message_id=message_id,
                    reason=reason or "not_deletable",
                    subject=str(record.get("subject") or ""),
                    size_bytes=(
                        int(record["size_bytes"])
                        if isinstance(record.get("size_bytes"), int)
                        and not isinstance(record.get("size_bytes"), bool)
                        else 0
                    ),
                )
            )
        else:
            candidates.append(candidate)
    return DeleteDryRunResult(
        candidates=tuple(candidates),
        exclusions=tuple(exclusions),
        total_size_bytes=sum(candidate.size_bytes for candidate in candidates),
    )


def _event(
    event_name: str,
    candidate: DeleteCandidate,
    mode: str,
    timestamp: str,
) -> dict[str, JSONValue]:
    return {
        "event": event_name,
        "account_id": candidate.account_id,
        "folder_raw_name": candidate.folder_raw_name,
        "uid": candidate.uid,
        "uidvalidity": candidate.uidvalidity,
        "mode": mode,
        "timestamp": timestamp,
    }


def _audit_entry(candidate: DeleteCandidate, mode: str, timestamp: str) -> dict[str, Any]:
    return {
        "occurred_at": timestamp,
        "operation": "remote_delete",
        "account_id": candidate.account_id,
        "message_id": candidate.message_id,
        "subject": candidate.subject,
        "size_bytes": candidate.size_bytes,
        "detail": f"mode={mode}; uid={candidate.uid}; folder={candidate.folder_raw_name}",
    }


def _commit_completed_state(
    repo: BaseMessageRepository,
    candidate: DeleteCandidate,
    mode: str,
    timestamp: str,
) -> None:
    repo.begin_batch()
    repo.update_remote_state(candidate.message_id, "deleted")
    repo.record_audit(_audit_entry(candidate, mode, timestamp))
    repo.commit_batch()


def _plan_items(
    plan: DeleteDryRunResult | Iterable[DeleteCandidate],
) -> tuple[DeleteCandidate, ...]:
    if isinstance(plan, DeleteDryRunResult):
        return plan.candidates
    return tuple(plan)


def execute(
    fetcher: BaseMailFetcher,
    repo: BaseMessageRepository,
    storage: BaseEmlStorage,
    manifest: BaseManifestWriter,
    *,
    plan: DeleteDryRunResult | Iterable[DeleteCandidate],
    mode: str = "trash",
    storage_state: RemoteDeleteGate,
    delete_batch_limit: int = DEFAULT_DELETE_BATCH_LIMIT,
) -> DeleteResult:
    """Execute a reviewed plan while recording recoverable operation states."""

    if not storage_state.is_remote_delete_allowed():
        raise StorageDetachedError("Remote deletion requires attached storage")
    if mode not in {"trash", "expunge"}:
        raise ValueError("mode must be 'trash' or 'expunge'")
    if delete_batch_limit <= 0:
        raise ValueError("delete_batch_limit must be positive")

    items = _plan_items(plan)
    if len(items) > delete_batch_limit:
        raise ValueError(f"delete plan exceeds the batch limit ({delete_batch_limit})")
    if mode == "expunge" and not fetcher.supports_uid_expunge():
        raise PermanentError("UID EXPUNGE is not supported by this IMAP server")
    if mode == "trash" and items and fetcher.find_trash_folder() is None:
        raise PermanentError("could not identify the remote trash folder")

    completed_ids: list[Any] = []
    uncertain_ids: list[Any] = []
    skipped_ids: list[Any] = []
    errors: list[tuple[Any, str]] = []
    total_size_bytes = 0

    for planned in items:
        record = repo.get_message(planned.message_id)
        if record is None:
            skipped_ids.append(planned.message_id)
            errors.append((planned.message_id, "message_not_found"))
            continue
        candidate, reason = _candidate_from_record(repo, storage, record)
        if candidate is None:
            skipped_ids.append(planned.message_id)
            errors.append((planned.message_id, reason or "not_deletable"))
            continue
        if (
            candidate.file_hash != planned.file_hash
            or candidate.relative_path != planned.relative_path
            or candidate.uid != planned.uid
            or candidate.uidvalidity != planned.uidvalidity
            or candidate.folder_raw_name != planned.folder_raw_name
        ):
            skipped_ids.append(planned.message_id)
            errors.append((planned.message_id, "plan_stale"))
            continue

        timestamp = _timestamp()
        manifest.append(_event("remote_delete_intent", candidate, mode, timestamp))
        manifest.flush_and_sync()
        try:
            fetcher.delete_remote_message(candidate.folder_raw_name, candidate.uid, mode=mode)
        except (TransientError, StorageDetachedError) as error:
            manifest.append(_event("remote_delete_uncertain", candidate, mode, _timestamp()))
            manifest.flush_and_sync()
            uncertain_ids.append(candidate.message_id)
            errors.append((candidate.message_id, str(error)))
            continue
        except FetchError as error:
            skipped_ids.append(candidate.message_id)
            errors.append((candidate.message_id, str(error)))
            continue

        completed_timestamp = _timestamp()
        manifest.append(_event("remote_delete_completed", candidate, mode, completed_timestamp))
        manifest.flush_and_sync()
        _commit_completed_state(repo, candidate, mode, completed_timestamp)
        completed_ids.append(candidate.message_id)
        total_size_bytes += candidate.size_bytes

    return DeleteResult(
        completed_ids=tuple(completed_ids),
        uncertain_ids=tuple(uncertain_ids),
        skipped_ids=tuple(skipped_ids),
        errors=tuple(errors),
        total_size_bytes=total_size_bytes,
    )


def reconcile_uncertain_deletes(
    fetcher: BaseMailFetcher,
    repo: BaseMessageRepository,
    manifest: BaseManifestReader | BaseManifestWriter,
    *,
    storage_state: RemoteDeleteGate,
) -> None:
    """Confirm uncertain operations only when the original UID is gone."""

    if not storage_state.is_remote_delete_allowed():
        raise StorageDetachedError("Remote-delete reconciliation requires attached storage")
    reader_object = (
        manifest
        if callable(getattr(manifest, "read_incomplete_intents", None))
        else getattr(manifest, "reader", None)
    )
    writer_object = (
        manifest
        if callable(getattr(manifest, "append", None))
        else getattr(manifest, "writer", None)
    )
    if not callable(getattr(reader_object, "read_incomplete_intents", None)):
        raise TypeError("manifest must expose a readable manifest")
    if not callable(getattr(writer_object, "append", None)):
        raise TypeError("manifest must also expose a writable manifest")
    reader = cast(BaseManifestReader, reader_object)
    writer = cast(BaseManifestWriter, writer_object)

    for intent in reader.read_incomplete_intents():
        if intent.get("event") != "remote_delete_intent":
            continue
        account_id = intent.get("account_id")
        folder_raw_name = intent.get("folder_raw_name")
        uid = intent.get("uid")
        uidvalidity = intent.get("uidvalidity")
        mode = intent.get("mode")
        if (
            not isinstance(account_id, str)
            or not isinstance(folder_raw_name, str)
            or not isinstance(uid, int)
            or isinstance(uid, bool)
            or not isinstance(uidvalidity, int)
            or isinstance(uidvalidity, bool)
            or mode not in {"trash", "expunge"}
        ):
            _LOGGER.warning("Ignoring malformed remote-delete intent during reconciliation")
            continue
        try:
            current_uidvalidity = fetcher.select_folder(folder_raw_name)
            if current_uidvalidity != uidvalidity:
                continue
            if uid in fetcher.list_existing_uids(folder_raw_name):
                continue
        except (TransientError, StorageDetachedError, FetchError):
            continue

        folder_id = _folder_id(repo, account_id, folder_raw_name)
        record = _message_by_uid(repo, account_id, folder_id, uidvalidity, uid)
        if record is None:
            continue
        candidate = _candidate_for_reconciliation(
            record, account_id, folder_raw_name, uid, uidvalidity
        )
        if candidate is None:
            continue
        timestamp = _timestamp()
        writer.append(_event("remote_delete_completed", candidate, str(mode), timestamp))
        writer.flush_and_sync()
        _commit_completed_state(repo, candidate, str(mode), timestamp)


def _folder_id(repo: BaseMessageRepository, account_id: str, raw_name: str) -> Any:
    for folder in repo.list_folders(account_id):
        if folder.get("raw_name") == raw_name:
            return folder.get("id")
    return None


def _message_by_uid(
    repo: BaseMessageRepository,
    account_id: str,
    folder_id: Any,
    uidvalidity: int,
    uid: int,
) -> MessageRecord | None:
    for record in repo.list_stored_messages(account_id):
        if (
            record.get("folder_id") == folder_id
            and record.get("uidvalidity") == uidvalidity
            and record.get("uid") == uid
        ):
            return record
    return None


def _candidate_for_reconciliation(
    record: MessageRecord,
    account_id: str,
    folder_raw_name: str,
    uid: int,
    uidvalidity: int,
) -> DeleteCandidate | None:
    message_id = record.get("id")
    size_bytes = record.get("size_bytes")
    relative_path = record.get("relative_path")
    file_hash = record.get("file_hash")
    if (
        message_id is None
        or not isinstance(size_bytes, int)
        or isinstance(size_bytes, bool)
        or not isinstance(relative_path, str)
        or not isinstance(file_hash, str)
    ):
        return None
    return DeleteCandidate(
        message_id=message_id,
        account_id=account_id,
        folder_raw_name=folder_raw_name,
        uid=uid,
        uidvalidity=uidvalidity,
        subject=str(record.get("subject") or ""),
        date_sent=record.get("date_sent") if isinstance(record.get("date_sent"), str) else None,
        internal_date=(
            record.get("internal_date") if isinstance(record.get("internal_date"), str) else None
        ),
        size_bytes=size_bytes,
        relative_path=relative_path,
        file_hash=file_hash,
        message_id_header=(
            record.get("message_id") if isinstance(record.get("message_id"), str) else None
        ),
    )


__all__ = [
    "DEFAULT_DELETE_BATCH_LIMIT",
    "DeleteCandidate",
    "DeleteDryRunResult",
    "DeleteExclusion",
    "DeleteResult",
    "dry_run",
    "execute",
    "reconcile_uncertain_deletes",
]
