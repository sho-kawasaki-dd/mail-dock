"""Verify the durable EML set without coupling use cases to storage details."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from mail_dock.domain.errors import ManifestCorruptError, OperationCancelledError
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.ports import BaseIntegrityStorage, BaseManifestReader, JSONValue
from mail_dock.domain.repository import BaseMessageRepository, MessageRecord

_LOGGER = logging.getLogger(__name__)
_CRC_SEPARATOR = b"|CRC32:"


@dataclass(frozen=True)
class VerificationIssue:
    """One EML whose metadata or content failed an integrity check."""

    relative_path: str
    reason: str
    message_id: Any | None = None
    expected_size: int | None = None
    actual_size: int | None = None
    expected_hash: str | None = None
    actual_hash: str | None = None


@dataclass(frozen=True)
class VerifyProgress:
    """Progress for a bounded full-file or orphan scan."""

    checked_count: int
    total_count: int
    current_path: str


@dataclass(frozen=True)
class QuickVerifyResult:
    checked_count: int
    missing_paths: tuple[str, ...]
    size_mismatch_paths: tuple[str, ...]
    cancelled: bool

    @property
    def missing_count(self) -> int:
        return len(self.missing_paths)

    @property
    def size_mismatch_count(self) -> int:
        return len(self.size_mismatch_paths)

    @property
    def mismatch_count(self) -> int:
        return self.size_mismatch_count


@dataclass(frozen=True)
class RangeVerifyResult:
    checked_count: int
    issues: tuple[VerificationIssue, ...]
    repaired_count: int
    quarantined_count: int
    cancelled: bool

    @property
    def mismatch_count(self) -> int:
        return sum(issue.reason == "hash_mismatch" for issue in self.issues)

    @property
    def missing_count(self) -> int:
        return sum(issue.reason == "missing" for issue in self.issues)


@dataclass(frozen=True)
class FullVerifyResult:
    checked_count: int
    issues: tuple[VerificationIssue, ...]
    cancelled: bool

    @property
    def mismatch_count(self) -> int:
        return sum(issue.reason != "orphan" for issue in self.issues)


@dataclass(frozen=True)
class OrphanCandidate:
    relative_path: str
    file_hash: str
    source_item_key: str


@dataclass(frozen=True)
class OrphanScanResult:
    checked_count: int
    registerable: tuple[OrphanCandidate, ...]
    quarantined_paths: tuple[str, ...]
    cancelled: bool

    @property
    def registerable_paths(self) -> tuple[str, ...]:
        return tuple(candidate.relative_path for candidate in self.registerable)

    @property
    def orphan_count(self) -> int:
        return len(self.registerable) + len(self.quarantined_paths)


@dataclass(frozen=True)
class ManifestVerifyResult:
    files_checked: int
    records_checked: int
    repaired_bytes: int
    cancelled: bool

    @property
    def repaired_count(self) -> int:
        return int(self.repaired_bytes > 0)


def _token(cancel: CancelToken | None) -> CancelToken:
    return cancel or CancelToken()


def _record_path(record: Mapping[str, Any]) -> str | None:
    value = record.get("relative_path")
    return value if isinstance(value, str) and value else None


def _record_hash(record: Mapping[str, Any]) -> str | None:
    value = record.get("file_hash")
    return value if isinstance(value, str) and value else None


def _hash_chunks(
    storage: BaseIntegrityStorage,
    relative_path: str,
    cancel: CancelToken | None = None,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    size_bytes = 0
    for chunk in storage.iter_chunks(relative_path):
        if cancel is not None:
            cancel.raise_if_cancelled()
        digest.update(chunk)
        size_bytes += len(chunk)
    return digest.hexdigest(), size_bytes


def _safe_progress(
    on_progress: Callable[[VerifyProgress], None] | None,
    checked_count: int,
    total_count: int,
    relative_path: str,
) -> None:
    if on_progress is not None:
        on_progress(VerifyProgress(checked_count, total_count, relative_path))


def quick_verify(
    repo: BaseMessageRepository,
    storage: BaseIntegrityStorage,
    *,
    cancel: CancelToken | None = None,
) -> QuickVerifyResult:
    """Check every stored message's path and recorded byte size."""

    token = _token(cancel)
    checked_count = 0
    missing_paths: list[str] = []
    size_mismatch_paths: list[str] = []
    cancelled = False

    for record in repo.list_stored_messages():
        try:
            token.raise_if_cancelled()
        except OperationCancelledError:
            cancelled = True
            break
        relative_path = _record_path(record)
        if relative_path is None:
            continue
        checked_count += 1
        try:
            file_stat = storage.stat(relative_path)
        except FileNotFoundError:
            missing_paths.append(relative_path)
            continue
        expected_size = record.get("size_bytes")
        if isinstance(expected_size, int) and file_stat.st_size != expected_size:
            size_mismatch_paths.append(relative_path)

    return QuickVerifyResult(
        checked_count,
        tuple(missing_paths),
        tuple(size_mismatch_paths),
        cancelled,
    )


def _records_by_source_key(repo: BaseMessageRepository) -> dict[tuple[str, str], MessageRecord]:
    records: dict[tuple[str, str], MessageRecord] = {}
    for record in repo.list_stored_messages():
        account_id = record.get("account_id")
        source_item_key = record.get("source_item_key")
        if isinstance(account_id, str) and isinstance(source_item_key, str):
            records[(account_id, source_item_key)] = record
    return records


def _record_failure(repo: BaseMessageRepository, record: Mapping[str, Any], reason: str) -> None:
    account_id = record.get("account_id")
    folder_id = record.get("folder_id")
    uidvalidity = record.get("uidvalidity")
    uid = record.get("uid")
    if (
        isinstance(account_id, str)
        and folder_id is not None
        and isinstance(uidvalidity, int)
        and isinstance(uid, int)
    ):
        repo.record_failure(account_id, folder_id, uidvalidity, uid, "integrity", reason)


def _repair_integrity_issue(
    repo: BaseMessageRepository,
    storage: BaseIntegrityStorage,
    record: Mapping[str, Any] | None,
    issue: VerificationIssue,
) -> int:
    if record is None:
        return 0
    _record_failure(repo, record, issue.reason)
    message_id = record.get("id")
    if message_id is None:
        return 0
    if issue.reason == "hash_mismatch":
        storage.quarantine(issue.relative_path)
    repo.update_message_storage(message_id, None, None)
    return 1


def range_verify(
    repo: BaseMessageRepository,
    storage: BaseIntegrityStorage,
    manifest_reader: BaseManifestReader,
    *,
    cancel: CancelToken | None = None,
) -> RangeVerifyResult:
    """Re-hash only EMLs referenced after the latest durable checkpoint."""

    token = _token(cancel)
    records_by_key = _records_by_source_key(repo)
    checked_count = 0
    repaired_count = 0
    quarantined_count = 0
    issues: list[VerificationIssue] = []
    cancelled = False

    for event in manifest_reader.read_events_since_checkpoint():
        if event.get("event") != "fetch":
            continue
        try:
            token.raise_if_cancelled()
        except OperationCancelledError:
            cancelled = True
            break
        account_id = event.get("account_id")
        source_item_key = event.get("source_item_key")
        relative_path = event.get("relative_path")
        expected_hash = event.get("file_hash")
        if not isinstance(account_id, str):
            continue
        if not isinstance(source_item_key, str):
            continue
        if not isinstance(relative_path, str):
            continue
        if not isinstance(expected_hash, str):
            continue
        record = records_by_key.get((account_id, source_item_key))
        checked_count += 1
        try:
            actual_hash, _ = _hash_chunks(storage, relative_path, token)
        except OperationCancelledError:
            cancelled = True
            break
        except FileNotFoundError:
            issue = VerificationIssue(
                relative_path,
                "missing",
                None if record is None else record.get("id"),
                expected_hash=expected_hash,
            )
        else:
            if actual_hash != expected_hash.casefold():
                issue = VerificationIssue(
                    relative_path,
                    "hash_mismatch",
                    None if record is None else record.get("id"),
                    expected_hash=expected_hash,
                    actual_hash=actual_hash,
                )
            else:
                continue
        issues.append(issue)
        if issue.reason == "hash_mismatch":
            if record is None:
                storage.quarantine(issue.relative_path)
            quarantined_count += 1
        if record is not None:
            repaired_count += _repair_integrity_issue(repo, storage, record, issue)

    return RangeVerifyResult(
        checked_count,
        tuple(issues),
        repaired_count,
        quarantined_count,
        cancelled,
    )


def full_verify(
    repo: BaseMessageRepository,
    storage: BaseIntegrityStorage,
    *,
    cancel: CancelToken | None = None,
    on_progress: Callable[[VerifyProgress], None] | None = None,
) -> FullVerifyResult:
    """Hash every EML using bounded chunks and compare known DB records."""

    token = _token(cancel)
    paths = tuple(storage.iter_eml_paths())
    records_by_path = {
        relative_path: record
        for record in repo.list_stored_messages()
        if (relative_path := _record_path(record)) is not None
    }
    issues: list[VerificationIssue] = []
    cancelled = False

    checked_count = 0
    for relative_path in paths:
        try:
            token.raise_if_cancelled()
        except OperationCancelledError:
            cancelled = True
            break
        checked_count += 1
        try:
            actual_hash, actual_size = _hash_chunks(storage, relative_path, token)
        except OperationCancelledError:
            cancelled = True
            break
        record = records_by_path.get(relative_path)
        expected_hash = _record_hash(record) if record is not None else None
        expected_size = record.get("size_bytes") if record is not None else None
        if record is None:
            filename = relative_path.rsplit("/", 1)[-1]
            if filename.casefold() != f"{actual_hash[:32]}.eml".casefold():
                issues.append(
                    VerificationIssue(
                        relative_path,
                        "orphan_hash_mismatch",
                        expected_hash=filename.rsplit(".", 1)[0],
                        actual_hash=actual_hash,
                        actual_size=actual_size,
                    )
                )
        elif expected_hash is not None and actual_hash != expected_hash.casefold():
            issues.append(
                VerificationIssue(
                    relative_path,
                    "hash_mismatch",
                    record.get("id"),
                    expected_size=expected_size if isinstance(expected_size, int) else None,
                    actual_size=actual_size,
                    expected_hash=expected_hash,
                    actual_hash=actual_hash,
                )
            )
        elif isinstance(expected_size, int) and actual_size != expected_size:
            issues.append(
                VerificationIssue(
                    relative_path,
                    "size_mismatch",
                    record.get("id"),
                    expected_size=expected_size,
                    actual_size=actual_size,
                    expected_hash=expected_hash,
                    actual_hash=actual_hash,
                )
            )
        _safe_progress(on_progress, checked_count, len(paths), relative_path)

    return FullVerifyResult(checked_count, tuple(issues), cancelled)


def orphan_scan(
    repo: BaseMessageRepository,
    storage: BaseIntegrityStorage,
    *,
    cancel: CancelToken | None = None,
    on_progress: Callable[[VerifyProgress], None] | None = None,
    manifest_reader: BaseManifestReader | None = None,
) -> OrphanScanResult:
    """Find EMLs absent from the DB and quarantine those without provenance."""

    token = _token(cancel)
    paths = tuple(storage.iter_eml_paths())
    known_paths = {
        relative_path
        for record in repo.list_stored_messages()
        if (relative_path := _record_path(record)) is not None
    }
    known_hashes = {
        file_hash
        for record in repo.list_stored_messages()
        if (file_hash := _record_hash(record)) is not None
    }
    fetch_events: dict[str, Mapping[str, JSONValue]] = {}
    if manifest_reader is not None:
        for event in manifest_reader.read_all_events():
            if event.get("event") != "fetch":
                continue
            event_path = event.get("relative_path")
            if isinstance(event_path, str):
                fetch_events[event_path] = event

    registerable: list[OrphanCandidate] = []
    quarantined_paths: list[str] = []
    checked_count = 0
    cancelled = False
    for relative_path in paths:
        try:
            token.raise_if_cancelled()
        except OperationCancelledError:
            cancelled = True
            break
        checked_count += 1
        if relative_path in known_paths:
            _safe_progress(on_progress, checked_count, len(paths), relative_path)
            continue
        try:
            actual_hash, _ = _hash_chunks(storage, relative_path, token)
        except OperationCancelledError:
            cancelled = True
            break
        fetch_event = fetch_events.get(relative_path)
        source_item_key = None if fetch_event is None else fetch_event.get("source_item_key")
        if (
            fetch_event is not None
            and fetch_event.get("file_hash") == actual_hash
            and isinstance(source_item_key, str)
            and actual_hash not in known_hashes
        ):
            registerable.append(OrphanCandidate(relative_path, actual_hash, source_item_key))
        else:
            storage.quarantine(relative_path)
            quarantined_paths.append(relative_path)
            repo.record_audit(
                {
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "operation": "orphan_quarantine",
                    "detail": relative_path,
                }
            )
            _LOGGER.warning("Quarantined EML without manifest provenance: %s", relative_path)
        _safe_progress(on_progress, checked_count, len(paths), relative_path)

    return OrphanScanResult(
        checked_count,
        tuple(registerable),
        tuple(quarantined_paths),
        cancelled,
    )


def _manifest_line_is_valid(line: bytes) -> Mapping[str, Any]:
    if not line.endswith(b"\n"):
        raise ManifestCorruptError("manifest record is missing its trailing newline")
    content = line[:-1]
    payload, separator, checksum = content.rpartition(_CRC_SEPARATOR)
    if not separator or len(checksum) != 8:
        raise ManifestCorruptError("manifest record has no valid CRC32 suffix")
    try:
        expected_checksum = int(checksum, 16)
    except ValueError as error:
        raise ManifestCorruptError("manifest record has an invalid CRC32 suffix") from error
    if zlib.crc32(payload) & 0xFFFFFFFF != expected_checksum:
        raise ManifestCorruptError("manifest record CRC32 does not match its payload")
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManifestCorruptError("manifest record contains invalid JSON") from error
    if not isinstance(decoded, dict) or not isinstance(decoded.get("event"), str):
        raise ManifestCorruptError("manifest record is not a JSON event object")
    return decoded


def _verify_manifest_file(path: Any) -> tuple[int, int]:
    offset = 0
    records_checked = 0
    truncate_at: int | None = None
    with path.open("rb") as manifest_file:
        line = manifest_file.readline()
        while line:
            next_line = manifest_file.readline()
            try:
                _manifest_line_is_valid(line)
            except ManifestCorruptError:
                if next_line:
                    raise
                truncate_at = offset
                break
            records_checked += 1
            offset += len(line)
            line = next_line
    if truncate_at is None:
        return records_checked, 0
    before = path.stat().st_size
    with path.open("r+b") as manifest_file:
        manifest_file.truncate(truncate_at)
        manifest_file.flush()
        os.fsync(manifest_file.fileno())
    return records_checked, before - truncate_at


def verify_manifest(root: Any, *, cancel: CancelToken | None = None) -> ManifestVerifyResult:
    """Validate manifest CRCs and repair only malformed final records."""

    token = _token(cancel)
    paths = tuple(sorted(root.expanduser().resolve().glob("manifests/imap/*/events-*.jsonl")))
    files_checked = 0
    records_checked = 0
    repaired_bytes = 0
    cancelled = False
    for path in paths:
        try:
            token.raise_if_cancelled()
        except OperationCancelledError:
            cancelled = True
            break
        file_records, file_repaired_bytes = _verify_manifest_file(path)
        files_checked += 1
        records_checked += file_records
        repaired_bytes += file_repaired_bytes
    return ManifestVerifyResult(files_checked, records_checked, repaired_bytes, cancelled)


__all__ = [
    "FullVerifyResult",
    "ManifestVerifyResult",
    "OrphanCandidate",
    "OrphanScanResult",
    "QuickVerifyResult",
    "RangeVerifyResult",
    "VerificationIssue",
    "VerifyProgress",
    "full_verify",
    "orphan_scan",
    "quick_verify",
    "range_verify",
    "verify_manifest",
]