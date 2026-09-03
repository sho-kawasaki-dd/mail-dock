"""Durable account and folder snapshots used by the manifest and reindex flows."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime

from mail_dock.domain.ports import (
    BaseIntegrityStorage,
    BaseManifestReader,
    BaseManifestWriter,
    BasePurgeStorage,
    JSONValue,
)
from mail_dock.domain.repository import BaseMessageRepository, MessageRecord
from mail_dock.usecases.trash import StorageWriteGate, recover_incomplete_purges
from mail_dock.usecases.verify import RangeVerifyResult, range_verify

_ACCOUNT_FIELDS = (
    "account_id",
    "provider_type",
    "display_name",
    "host",
    "port",
    "username",
    "is_enabled",
)
_FOLDER_FIELDS = (
    "account_id",
    "folder_raw_name",
    "display_name",
    "uidvalidity",
    "delimiter",
    "is_sync_target",
)


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _account_event(account: MessageRecord) -> dict[str, JSONValue]:
    account_id = str(account.get("id", account.get("account_id", "")))
    return {
        "event": "account_snapshot",
        "account_id": account_id,
        "provider_type": str(account.get("provider_type", "onamae_imap")),
        "display_name": account.get("display_name"),
        "host": str(account.get("host", "")),
        "port": int(account.get("port", 993)),
        "username": str(account.get("username", "")),
        "is_enabled": bool(account.get("is_enabled", True)),
        "timestamp": _timestamp(),
    }


def folder_snapshot_event(account_id: str, folder: MessageRecord) -> dict[str, JSONValue]:
    """Build a `folder_snapshot` event from a folder record's current attributes."""

    uidvalidity = folder.get("uidvalidity")
    delimiter = folder.get("delimiter")
    return {
        "event": "folder_snapshot",
        "account_id": account_id,
        "folder_raw_name": str(folder.get("raw_name", "")),
        "display_name": str(folder.get("display_name", folder.get("raw_name", ""))),
        "uidvalidity": uidvalidity if isinstance(uidvalidity, int) else None,
        "delimiter": delimiter if isinstance(delimiter, str) else None,
        "is_sync_target": bool(folder.get("is_sync_target", False)),
        "timestamp": _timestamp(),
    }


def _matches_previous(
    event: Mapping[str, JSONValue],
    previous: Mapping[str, JSONValue] | None,
    fields: tuple[str, ...],
) -> bool:
    return previous is not None and all(event.get(field) == previous.get(field) for field in fields)


def _previous_snapshot(
    reader: BaseManifestReader,
    event_name: str,
    identity_field: str,
    identity: str,
) -> Mapping[str, JSONValue] | None:
    previous: Mapping[str, JSONValue] | None = None
    for event in reader.read_all_events():
        if event.get("event") == event_name and event.get(identity_field) == identity:
            previous = event
    return previous


def record_account_snapshot(
    writer: BaseManifestWriter,
    reader: BaseManifestReader,
    account: MessageRecord,
    *,
    only_if_missing: bool = False,
) -> bool:
    """Record an account's non-secret state when it changed."""

    event = _account_event(account)
    previous = _previous_snapshot(
        reader, "account_snapshot", "account_id", str(event["account_id"])
    )
    if previous is not None and (
        only_if_missing or _matches_previous(event, previous, _ACCOUNT_FIELDS)
    ):
        return False
    writer.append(event)
    writer.flush_and_sync()
    return True


def record_folder_snapshot(
    writer: BaseManifestWriter,
    reader: BaseManifestReader,
    account_id: str,
    folder: MessageRecord,
    *,
    only_if_missing: bool = False,
) -> bool:
    """Record a folder's current attributes when they changed."""

    event = folder_snapshot_event(account_id, folder)
    previous = _previous_snapshot(
        reader,
        "folder_snapshot",
        "folder_raw_name",
        str(event["folder_raw_name"]),
    )
    if previous is not None and (
        only_if_missing or _matches_previous(event, previous, _FOLDER_FIELDS)
    ):
        return False
    writer.append(event)
    writer.flush_and_sync()
    return True


def backfill_snapshots(
    repo: BaseMessageRepository,
    manifest_writer_factory: Callable[[str], BaseManifestWriter],
    manifest_reader_factory: Callable[[str], BaseManifestReader],
) -> tuple[int, int]:
    """Backfill one account and its folders only when no snapshot exists."""

    account_count = 0
    folder_count = 0
    for account in repo.list_accounts():
        account_id = str(account.get("id", account.get("account_id", "")))
        if not account_id:
            continue
        writer = manifest_writer_factory(account_id)
        try:
            reader = manifest_reader_factory(account_id)
            if record_account_snapshot(writer, reader, account, only_if_missing=True):
                account_count += 1
            for folder in repo.list_folders(account_id):
                if record_folder_snapshot(
                    writer,
                    reader,
                    account_id,
                    folder,
                    only_if_missing=True,
                ):
                    folder_count += 1
        finally:
            writer.close()
    return account_count, folder_count


def repair_manifest_tails(
    repo: BaseMessageRepository,
    manifest_reader_factory: Callable[[str], BaseManifestReader],
) -> int:
    """Consume each account manifest so a torn final record is repaired."""

    repaired_manifests = 0
    for account in repo.list_accounts():
        account_id = str(account.get("id", account.get("account_id", "")))
        if not account_id:
            continue
        reader = manifest_reader_factory(account_id)
        had_events = False
        for _event in reader.read_all_events():
            had_events = True
        if had_events:
            repaired_manifests += 1
    return repaired_manifests


class _AccountRecoveryManifest(BaseManifestReader):
    """Pair one account's manifest reader and writer for purge recovery.

    ``recover_incomplete_purges`` only writes the completion half of an
    already-started intent, so it needs both sides of one account's manifest
    even though its parameter is typed as a reader (see ``BaseManifestReader``
    usage in ``usecases/trash.py``).
    """

    def __init__(self, reader: BaseManifestReader, writer: BaseManifestWriter) -> None:
        self._reader = reader
        self.writer = writer

    def read_all_events(self) -> Iterator[Mapping[str, JSONValue]]:
        return self._reader.read_all_events()

    def read_last_checkpoint(self) -> Mapping[str, JSONValue] | None:
        return self._reader.read_last_checkpoint()

    def read_events_since_checkpoint(self) -> Iterator[Mapping[str, JSONValue]]:
        return self._reader.read_events_since_checkpoint()

    def read_incomplete_intents(self) -> Iterator[Mapping[str, JSONValue]]:
        return self._reader.read_incomplete_intents()


def recover_after_unclean_shutdown(
    repo: BaseMessageRepository,
    integrity_storage: BaseIntegrityStorage,
    purge_storage: BasePurgeStorage,
    manifest_reader_factory: Callable[[str], BaseManifestReader],
    manifest_writer_factory: Callable[[str], BaseManifestWriter],
    *,
    storage_state: StorageWriteGate,
) -> tuple[RangeVerifyResult, ...]:
    """Run the local, network-free startup recovery after a prior crash (F-6/F-13).

    For each account this re-hashes EMLs written since the last durable
    checkpoint (repairing and quarantining mismatches) and resumes any
    ``purge_intent`` left without its completing ``purged`` event. Callers
    should run this only after ``repair_manifest_tails`` has already made the
    manifest tails safe to read. ``remote_delete_intent`` reconciliation is
    intentionally excluded: it requires a live IMAP connection and stays on
    the regular sync path instead of blocking startup.
    """

    results: list[RangeVerifyResult] = []
    for account in repo.list_accounts():
        account_id = str(account.get("id", account.get("account_id", "")))
        if not account_id:
            continue
        reader = manifest_reader_factory(account_id)
        results.append(range_verify(repo, integrity_storage, reader))
        manifest = _AccountRecoveryManifest(reader, manifest_writer_factory(account_id))
        try:
            recover_incomplete_purges(
                repo,
                purge_storage,
                manifest,
                storage_state=storage_state,
            )
        finally:
            manifest.writer.close()
    return tuple(results)
