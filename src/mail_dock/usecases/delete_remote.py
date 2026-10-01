"""Safety-first use cases for deleting messages from an IMAP server."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from mail_dock.domain.errors import (
    FetchError,
    PermanentError,
    StorageDetachedError,
    StorageError,
    TransientError,
    UidValidityChanged,
)
from mail_dock.domain.fetcher import BaseMailFetcher, RemoteMoveResult
from mail_dock.domain.imap_flags import has_imap_flag
from mail_dock.domain.message_identity import imap_source_item_key
from mail_dock.domain.ports import BaseEmlStorage, BaseManifestReader, BaseManifestWriter, JSONValue
from mail_dock.domain.repository import BaseMessageRepository, MessageRecord
from mail_dock.domain.search import MessageSummary
from mail_dock.usecases.account_guards import ensure_imap_account, ensure_imap_message

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
    folder_id: Any | None = None

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
class DeleteScope:
    """Immutable list-wide selection and exclusions for remote deletion."""

    message_ids: tuple[int, ...]
    flagged_message_ids: tuple[int, ...]
    non_deletable_message_ids: tuple[int, ...]
    matched_count: int
    flagged_excluded_count: int
    truncated: bool
    delete_batch_limit: int


@dataclass(frozen=True)
class DeleteDryRunResult:
    """Reviewable remote-delete plan produced without changing the server."""

    candidates: tuple[DeleteCandidate, ...] = ()
    exclusions: tuple[DeleteExclusion, ...] = ()
    total_size_bytes: int = 0
    exclude_flagged: bool = False
    scope: DeleteScope | None = None

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
    *,
    folder_id: Any | None = None,
    exclude_flagged: bool = False,
    require_present: bool = False,
) -> tuple[DeleteCandidate | None, str | None]:
    source_item_key = record.get("source_item_key")
    account_id_value = record.get("account_id")
    if isinstance(source_item_key, str) and isinstance(account_id_value, str):
        memberships = repo.list_message_memberships(account_id_value, source_item_key)
        if folder_id is not None:
            memberships = tuple(
                membership for membership in memberships if membership.get("folder_id") == folder_id
            )
            if len(memberships) != 1:
                return None, "membership_not_found"
        elif len(memberships) > 1:
            return None, "folder_selection_required"
        if memberships:
            record = {**record, **memberships[0]}

    message_id = record.get("id")
    subject = record.get("subject")
    subject_text = subject if isinstance(subject, str) else ""
    size_bytes = record.get("size_bytes")
    if message_id is None:
        return None, "message_id_missing"
    remote_state = record.get("remote_state")
    if require_present and remote_state != "present":
        return None, "remote_state_not_deletable"
    if remote_state in {"deleted", "uncertain"}:
        return None, "remote_state_not_deletable"
    imap_flags = record.get("imap_flags")
    if exclude_flagged and has_imap_flag(
        imap_flags if isinstance(imap_flags, str) else None, r"\Flagged"
    ):
        return None, "flagged"

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
            folder_id=record.get("folder_id"),
        ),
        None,
    )


def dry_run(
    repo: BaseMessageRepository,
    storage: BaseEmlStorage,
    *,
    message_ids: Iterable[Any],
    storage_state: RemoteDeleteGate,
    folder_id: Any | None = None,
    exclude_flagged: bool = False,
    scope: DeleteScope | None = None,
) -> DeleteDryRunResult:
    """Build a deletion plan after verifying every local prerequisite."""

    selected_message_ids = tuple(message_ids)
    selected_records = [repo.get_message(message_id) for message_id in selected_message_ids]
    for record in selected_records:
        if record is not None:
            ensure_imap_message(repo, record)
    if not storage_state.is_remote_delete_allowed():
        raise StorageDetachedError("Remote deletion requires attached storage")

    candidates: list[DeleteCandidate] = []
    exclusions: list[DeleteExclusion] = []
    fixed_exclusions = (
        {
            **dict.fromkeys(scope.flagged_message_ids, "flagged"),
            **dict.fromkeys(scope.non_deletable_message_ids, "remote_state_not_deletable"),
        }
        if scope is not None
        else {}
    )
    eligible_ids = set(scope.message_ids) if scope is not None else None
    for message_id, record in zip(selected_message_ids, selected_records, strict=True):
        fixed_reason = fixed_exclusions.get(message_id)
        if fixed_reason is not None:
            exclusions.append(
                DeleteExclusion(
                    message_id=message_id,
                    reason=fixed_reason,
                    subject=str(record.get("subject") or "") if record is not None else "",
                    size_bytes=(
                        int(record["size_bytes"])
                        if record is not None
                        and isinstance(record.get("size_bytes"), int)
                        and not isinstance(record.get("size_bytes"), bool)
                        else 0
                    ),
                )
            )
            continue
        if eligible_ids is not None and message_id not in eligible_ids:
            exclusions.append(DeleteExclusion(message_id, "outside_delete_scope"))
            continue
        if record is None:
            exclusions.append(DeleteExclusion(message_id, "message_not_found"))
            continue
        candidate, reason = _candidate_from_record(
            repo,
            storage,
            record,
            folder_id=folder_id,
            exclude_flagged=exclude_flagged,
            require_present=scope is not None,
        )
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
        exclude_flagged=exclude_flagged,
        scope=scope,
    )


def select_delete_scope(
    summaries: Iterable[MessageSummary], *, exclude_flagged: bool, limit: int
) -> DeleteScope:
    """Select the oldest deletable rows up to the confirmed operation limit."""

    if limit <= 0:
        raise ValueError("limit must be positive")
    items = tuple(summaries)
    non_deletable = tuple(item.id for item in items if item.remote_state != "present")
    non_deletable_set = set(non_deletable)
    flagged = tuple(
        item.id
        for item in items
        if item.id not in non_deletable_set
        and exclude_flagged
        and has_imap_flag(item.imap_flags, r"\Flagged")
    )
    excluded_ids = non_deletable_set | set(flagged)
    eligible = sorted(
        (item for item in items if item.id not in excluded_ids),
        key=_delete_scope_sort_key,
    )
    message_ids = tuple(item.id for item in eligible[:limit])
    truncated = len(items) - len(non_deletable) - len(flagged) - len(message_ids) > 0
    return DeleteScope(
        message_ids=message_ids,
        flagged_message_ids=flagged,
        non_deletable_message_ids=non_deletable,
        matched_count=len(items),
        flagged_excluded_count=len(flagged),
        truncated=truncated,
        delete_batch_limit=limit,
    )


def _delete_scope_sort_key(item: MessageSummary) -> tuple[int, datetime, int]:
    value = item.date_sent if item.date_sent is not None else item.internal_date
    if value is None:
        return 0, datetime.min.replace(tzinfo=UTC), item.id
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return 1, normalized, item.id


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
    record: MessageRecord,
    *,
    merge_plan: _TrashMergePlan | None = None,
) -> None:
    repo.begin_batch()
    if mode == "remove_membership":
        source_item_key = record.get("source_item_key")
        if not isinstance(source_item_key, str) or not source_item_key:
            raise StorageError("Gmail membership removal has no canonical source key")
        remaining = [
            membership
            for membership in repo.list_message_memberships(candidate.account_id, source_item_key)
            if membership.get("folder_id") != candidate.folder_id
        ]
        repo.replace_message_memberships(candidate.message_id, remaining)
    elif merge_plan is not None:
        if merge_plan.duplicate_message_id is not None:
            repo.merge_duplicate_message(
                candidate.account_id, candidate.message_id, merge_plan.duplicate_message_id
            )
            repo.update_remote_state(
                candidate.message_id, "moved", merge_plan.trash_folder_id, candidate.folder_id
            )
        elif merge_plan.new_membership is not None:
            repo.replace_message_memberships(candidate.message_id, merge_plan.new_membership)
            repo.add_message_identity_alias(
                candidate.account_id,
                merge_plan.alias_source_item_key,
                candidate.message_id,
                "copyuid",
            )
    else:
        repo.update_remote_state(candidate.message_id, "deleted", folder_id=candidate.folder_id)
    repo.record_audit(_audit_entry(candidate, mode, timestamp))
    repo.commit_batch()


@dataclass(frozen=True)
class _TrashMergePlan:
    """Durable facts needed to fold a COPYUID-confirmed MOVE into one row."""

    alias_source_item_key: str
    duplicate_message_id: Any | None
    identity_event: Mapping[str, JSONValue]
    snapshot_event: Mapping[str, JSONValue]
    new_membership: list[MessageRecord] | None
    trash_folder_id: Any


def _build_trash_merge_plan(
    repo: BaseMessageRepository,
    candidate: DeleteCandidate,
    record: MessageRecord,
    trash_folder_raw_name: str,
    move_result: RemoteMoveResult,
) -> _TrashMergePlan | None:
    """Compute the identity link needed when our own MOVE lands on a known UID.

    Returns ``None`` when the destination folder is not tracked locally,
    leaving conservative move estimation on the next sync as the fallback.
    """

    canonical_key = record.get("source_item_key")
    if not isinstance(canonical_key, str) or not canonical_key:
        return None
    alias_key = imap_source_item_key(
        trash_folder_raw_name, move_result.uidvalidity, move_result.uid
    )
    if alias_key == canonical_key:
        return None
    trash_folder_id = next(
        (
            folder.get("id")
            for folder in repo.list_folders(candidate.account_id)
            if folder.get("raw_name") == trash_folder_raw_name
        ),
        None,
    )
    if trash_folder_id is None:
        return None
    timestamp = _timestamp()
    identity_event: dict[str, JSONValue] = {
        "event": "message_identity_linked",
        "account_id": candidate.account_id,
        "canonical_source_item_key": canonical_key,
        "alias_source_item_key": alias_key,
        "evidence_kind": "copyuid",
        "file_hash": candidate.file_hash,
        "timestamp": timestamp,
    }
    remaining = [
        {
            **membership,
            "remote_state": (
                "moved"
                if membership.get("folder_id") == candidate.folder_id
                else membership.get("remote_state", "present")
            ),
            "moved_to_folder_id": (
                trash_folder_id
                if membership.get("folder_id") == candidate.folder_id
                else membership.get("moved_to_folder_id")
            ),
            "moved_to_folder_raw_name": (
                trash_folder_raw_name
                if membership.get("folder_id") == candidate.folder_id
                else membership.get("moved_to_folder_raw_name")
            ),
        }
        for membership in repo.list_message_memberships(candidate.account_id, canonical_key)
    ]
    new_trash_membership: dict[str, Any] = {
        "folder_id": trash_folder_id,
        "folder_raw_name": trash_folder_raw_name,
        "uid": move_result.uid,
        "uidvalidity": move_result.uidvalidity,
        "remote_state": "present",
        "moved_to_folder_id": None,
        "imap_flags": None,
        "flags_seen_at": None,
        "last_seen_at": timestamp,
    }
    snapshot_memberships = [
        {
            "folder_raw_name": membership.get("folder_raw_name"),
            "uid": membership.get("uid"),
            "uidvalidity": membership.get("uidvalidity"),
            "remote_state": membership.get("remote_state", "present"),
            "moved_to_folder_raw_name": membership.get("moved_to_folder_raw_name"),
            "imap_flags": membership.get("imap_flags"),
            "flags_seen_at": membership.get("flags_seen_at"),
            "last_seen_at": membership.get("last_seen_at"),
        }
        for membership in remaining
    ]
    snapshot_memberships.append(
        {
            "folder_raw_name": trash_folder_raw_name,
            "uid": move_result.uid,
            "uidvalidity": move_result.uidvalidity,
            "remote_state": "present",
            "moved_to_folder_raw_name": None,
            "imap_flags": None,
            "flags_seen_at": None,
            "last_seen_at": timestamp,
        }
    )
    snapshot_event: dict[str, JSONValue] = {
        "event": "message_membership_snapshot",
        "account_id": candidate.account_id,
        "source_item_key": canonical_key,
        "memberships": cast(list[JSONValue], snapshot_memberships),
        "timestamp": timestamp,
    }
    duplicate_message_id = repo.find_message_id_by_source_item_key(candidate.account_id, alias_key)
    return _TrashMergePlan(
        alias_source_item_key=alias_key,
        duplicate_message_id=duplicate_message_id,
        identity_event=identity_event,
        snapshot_event=snapshot_event,
        new_membership=(
            None if duplicate_message_id is not None else [*remaining, new_trash_membership]
        ),
        trash_folder_id=trash_folder_id,
    )


def _membership_snapshot_event(
    repo: BaseMessageRepository,
    candidate: DeleteCandidate,
    record: MessageRecord,
) -> dict[str, JSONValue]:
    source_item_key = record.get("source_item_key")
    if not isinstance(source_item_key, str) or not source_item_key:
        raise StorageError("Gmail membership removal has no canonical source key")
    folder_names = {
        folder.get("id"): folder.get("raw_name")
        for folder in repo.list_folders(candidate.account_id)
    }
    memberships = []
    for membership in repo.list_message_memberships(candidate.account_id, source_item_key):
        if membership.get("folder_id") == candidate.folder_id:
            continue
        folder_raw_name = membership.get("folder_raw_name") or folder_names.get(
            membership.get("folder_id")
        )
        if not isinstance(folder_raw_name, str) or not folder_raw_name:
            raise StorageError("Gmail membership snapshot has an unknown folder")
        memberships.append(
            cast(
                JSONValue,
                {
                    "folder_raw_name": folder_raw_name,
                    "uid": membership.get("uid"),
                    "uidvalidity": membership.get("uidvalidity"),
                    "remote_state": membership.get("remote_state", "present"),
                    "moved_to_folder_raw_name": membership.get("moved_to_folder_raw_name"),
                    "imap_flags": membership.get("imap_flags"),
                    "flags_seen_at": membership.get("flags_seen_at"),
                    "last_seen_at": membership.get("last_seen_at"),
                },
            )
        )
    snapshot: dict[str, JSONValue] = {
        "event": "message_membership_snapshot",
        "account_id": candidate.account_id,
        "source_item_key": source_item_key,
        "memberships": memberships,
        "timestamp": _timestamp(),
    }
    labels = record.get("gmail_labels")
    if isinstance(labels, str):
        try:
            labels = json.loads(labels)
        except ValueError as error:
            raise StorageError("Gmail labels are invalid in the local message record") from error
    if isinstance(labels, (list, tuple)) and all(isinstance(label, str) for label in labels):
        snapshot["gmail_labels"] = [label for label in labels if label != candidate.folder_raw_name]
    return snapshot


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
    exclude_flagged: bool = False,
) -> DeleteResult:
    """Execute a reviewed plan while recording recoverable operation states."""

    items = _plan_items(plan)
    for item in items:
        ensure_imap_account(repo, item.account_id)
    if not storage_state.is_remote_delete_allowed():
        raise StorageDetachedError("Remote deletion requires attached storage")
    if mode not in {"trash", "expunge", "remove_membership"}:
        raise ValueError("mode must be 'trash', 'expunge', or 'remove_membership'")
    if delete_batch_limit <= 0:
        raise ValueError("delete_batch_limit must be positive")

    if len(items) > delete_batch_limit:
        raise ValueError(f"delete plan exceeds the batch limit ({delete_batch_limit})")
    google_account_ids = {
        account.get("id")
        for account in repo.list_accounts()
        if account.get("oauth_provider") == "google"
    }
    if mode == "expunge":
        account_ids = {candidate.account_id for candidate in items}
        if account_ids & google_account_ids:
            raise PermanentError("Gmail accounts do not support remote expunge")
    if mode == "remove_membership":
        account_ids = {candidate.account_id for candidate in items}
        if not account_ids <= google_account_ids:
            raise PermanentError("remote membership removal is only supported for Gmail labels")
    if mode == "expunge" and not fetcher.supports_uid_expunge():
        raise PermanentError("UID EXPUNGE is not supported by this IMAP server")
    trash_folder = fetcher.find_trash_folder() if mode == "trash" and items else None
    if mode == "trash" and items and trash_folder is None:
        raise PermanentError("could not identify the remote trash folder")

    completed_ids: list[Any] = []
    uncertain_ids: list[Any] = []
    skipped_ids: list[Any] = []
    errors: list[tuple[Any, str]] = []
    total_size_bytes = 0

    items_to_delete = items
    invalidated_folders: set[str] = set()
    if exclude_flagged and items:
        grouped_items: dict[str, list[DeleteCandidate]] = {}
        for candidate in items:
            grouped_items.setdefault(candidate.folder_raw_name, []).append(candidate)
        verified_flags: dict[tuple[str, int], tuple[str, ...]] = {}
        pending_flag_updates: list[tuple[DeleteCandidate, str]] = []
        preflight_skips: dict[Any, str] = {}
        for raw_name, folder_items in grouped_items.items():
            uidvalidities = {candidate.uidvalidity for candidate in folder_items}
            if len(uidvalidities) != 1:
                preflight_skips.update(
                    (candidate.message_id, "uidvalidity_mismatch") for candidate in folder_items
                )
                continue
            expected_uidvalidity = next(iter(uidvalidities))
            try:
                if fetcher.select_folder(raw_name) != expected_uidvalidity:
                    preflight_skips.update(
                        (candidate.message_id, "uidvalidity_mismatch")
                        for candidate in folder_items
                    )
                    continue
                refs = tuple(
                    fetcher.iter_flags(
                        raw_name,
                        (candidate.uid for candidate in folder_items),
                        expected_uidvalidity=expected_uidvalidity,
                    )
                )
            except UidValidityChanged:
                preflight_skips.update(
                    (candidate.message_id, "uidvalidity_mismatch") for candidate in folder_items
                )
                continue
            except (TransientError, StorageDetachedError):
                raise
            except FetchError:
                preflight_skips.update(
                    (candidate.message_id, "flag_unverified") for candidate in folder_items
                )
                continue
            requested_uids = {candidate.uid for candidate in folder_items}
            for ref in refs:
                if ref.uid in requested_uids:
                    verified_flags[(raw_name, ref.uid)] = ref.flags

        items_to_delete_list: list[DeleteCandidate] = []
        for candidate in items:
            reason = preflight_skips.get(candidate.message_id)
            flags = verified_flags.get((candidate.folder_raw_name, candidate.uid))
            if reason is None and flags is None:
                reason = "flag_unverified"
            if reason is not None:
                skipped_ids.append(candidate.message_id)
                errors.append((candidate.message_id, reason))
                continue
            assert flags is not None
            pending_flag_updates.append((candidate, " ".join(flags)))
            if has_imap_flag(" ".join(flags), r"\Flagged"):
                skipped_ids.append(candidate.message_id)
                errors.append((candidate.message_id, "flagged_on_server"))
            else:
                items_to_delete_list.append(candidate)
        if pending_flag_updates:
            flags_seen_at = _timestamp()
            repo.begin_batch()
            for candidate, flags_text in pending_flag_updates:
                if candidate.folder_id is not None:
                    repo.update_flags(
                        candidate.account_id,
                        candidate.folder_id,
                        candidate.uidvalidity,
                        candidate.uid,
                        flags_text,
                        flags_seen_at,
                    )
            repo.commit_batch()
        items_to_delete = tuple(items_to_delete_list)

    for planned in items_to_delete:
        if planned.folder_raw_name in invalidated_folders:
            skipped_ids.append(planned.message_id)
            errors.append((planned.message_id, "uidvalidity_mismatch"))
            continue
        record = repo.get_message(planned.message_id)
        if record is None:
            skipped_ids.append(planned.message_id)
            errors.append((planned.message_id, "message_not_found"))
            continue
        verified_candidate, reason = _candidate_from_record(
            repo, storage, record, folder_id=planned.folder_id
        )
        if verified_candidate is None:
            skipped_ids.append(planned.message_id)
            errors.append((planned.message_id, reason or "not_deletable"))
            continue
        if (
            verified_candidate.file_hash != planned.file_hash
            or verified_candidate.relative_path != planned.relative_path
            or verified_candidate.uid != planned.uid
            or verified_candidate.uidvalidity != planned.uidvalidity
            or verified_candidate.folder_raw_name != planned.folder_raw_name
        ):
            skipped_ids.append(planned.message_id)
            errors.append((planned.message_id, "plan_stale"))
            continue
        candidate = verified_candidate

        timestamp = _timestamp()
        manifest.append(_event("remote_delete_intent", candidate, mode, timestamp))
        manifest.flush_and_sync()
        move_result: RemoteMoveResult | None = None
        try:
            if mode == "trash":
                if exclude_flagged:
                    move_result = fetcher.move_remote_message_to_trash(
                        candidate.folder_raw_name,
                        candidate.uid,
                        expected_uidvalidity=candidate.uidvalidity,
                    )
                else:
                    move_result = fetcher.move_remote_message_to_trash(
                        candidate.folder_raw_name, candidate.uid
                    )
            elif mode == "expunge":
                if exclude_flagged:
                    fetcher.expunge_remote_message(
                        candidate.folder_raw_name,
                        candidate.uid,
                        expected_uidvalidity=candidate.uidvalidity,
                    )
                else:
                    fetcher.expunge_remote_message(candidate.folder_raw_name, candidate.uid)
            else:
                if exclude_flagged:
                    fetcher.remove_remote_membership(
                        candidate.folder_raw_name,
                        candidate.uid,
                        expected_uidvalidity=candidate.uidvalidity,
                    )
                else:
                    fetcher.remove_remote_membership(candidate.folder_raw_name, candidate.uid)
        except UidValidityChanged:
            invalidated_folders.add(candidate.folder_raw_name)
            skipped_ids.append(candidate.message_id)
            errors.append((candidate.message_id, "uidvalidity_mismatch"))
            _LOGGER.info(
                "Skipping remaining remote deletes in changed folder %s",
                candidate.folder_raw_name,
            )
            continue
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

        merge_plan: _TrashMergePlan | None = None
        if (
            mode == "trash"
            and move_result is not None
            and trash_folder is not None
            and candidate.account_id not in google_account_ids
            and repo.supports_message_folders()
        ):
            merge_plan = _build_trash_merge_plan(
                repo, candidate, record, trash_folder.raw_name, move_result
            )

        completed_timestamp = _timestamp()
        manifest.append(_event("remote_delete_completed", candidate, mode, completed_timestamp))
        if mode == "remove_membership":
            manifest.append(_membership_snapshot_event(repo, candidate, record))
        elif merge_plan is not None:
            manifest.append(merge_plan.identity_event)
            manifest.append(merge_plan.snapshot_event)
        manifest.flush_and_sync()
        _commit_completed_state(
            repo, candidate, mode, completed_timestamp, record, merge_plan=merge_plan
        )
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
            or mode not in {"trash", "expunge", "remove_membership"}
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
        if mode == "remove_membership":
            writer.append(_membership_snapshot_event(repo, candidate, record))
        writer.flush_and_sync()
        _commit_completed_state(repo, candidate, str(mode), timestamp, record)


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
