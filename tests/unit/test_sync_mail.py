from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from mail_dock.domain.errors import (
    AuthenticationError,
    PermanentError,
    StorageError,
)
from mail_dock.domain.fetcher import CancelToken, RemoteFolder, RemoteMessageRef
from mail_dock.domain.messages import ParsedMessage, StoredEml
from mail_dock.domain.ports import BaseEmlStorage, BaseManifestWriter, JSONValue
from mail_dock.usecases.sync_mail import SyncOptions, SyncResult, force_fetch_message, sync_account
from tests.support.fake_fetcher import FakeFetcher, FakeMessage
from tests.support.in_memory_repository import InMemoryMessageRepository


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("flag_refresh_enabled", "yes"),
        ("flag_refresh_window_days", 0),
        ("flag_refresh_min_interval_seconds", 0),
    ],
)
def test_sync_options_reject_invalid_flag_refresh_settings(field_name: str, value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        SyncOptions(**dict[str, Any]({field_name: value}))


class MemoryStorage(BaseEmlStorage):
    def __init__(self, on_save: Callable[[], None] | None = None) -> None:
        self.raw_by_path: dict[str, bytes] = {}
        self.save_calls: list[int] = []
        self.on_save = on_save

    def save(self, account_id: str, internal_date: datetime | None, raw: bytes) -> StoredEml:
        del internal_date
        file_hash = hashlib.sha256(raw).hexdigest()
        relative_path = f"eml/{account_id}/{file_hash[:32]}.eml"
        self.raw_by_path[relative_path] = raw
        self.save_calls.append(len(raw))
        if self.on_save is not None:
            self.on_save()
        return StoredEml(relative_path, file_hash, len(raw))

    def reuse(self, relative_path: str, expected_hash: str) -> StoredEml | None:
        raw = self.raw_by_path.get(relative_path)
        if raw is None or hashlib.sha256(raw).hexdigest() != expected_hash:
            return None
        return StoredEml(relative_path, expected_hash, len(raw), deduplicated=True)

    def read(self, relative_path: str) -> bytes:
        return self.raw_by_path[relative_path]

    def read_verified(self, relative_path: str, expected_hash: str) -> bytes:
        raw = self.read(relative_path)
        if hashlib.sha256(raw).hexdigest() != expected_hash.casefold():
            raise StorageError("EML file hash does not match expected hash")
        return raw


class MemoryManifest(BaseManifestWriter):
    def __init__(self) -> None:
        self.events: list[Mapping[str, JSONValue]] = []
        self.sync_count = 0

    def append(self, event: Mapping[str, JSONValue]) -> None:
        self.events.append(dict(event))

    def flush_and_sync(self) -> None:
        self.sync_count += 1

    def checkpoint(self, sequence: int, batch_id: str) -> None:
        self.append(
            {
                "event": "checkpoint",
                "account_id": "account",
                "timestamp": datetime.now(UTC).isoformat(),
                "sequence": sequence,
                "batch_id": batch_id,
            }
        )
        self.flush_and_sync()


class TrackingRepository(InMemoryMessageRepository):
    def __init__(self) -> None:
        super().__init__()
        self.operations: list[str] = []

    def commit_batch(self) -> None:
        self.operations.append("db_commit")
        super().commit_batch()


class TrackingManifest(MemoryManifest):
    def __init__(self, operations: list[str]) -> None:
        super().__init__()
        self.operations = operations

    def checkpoint(self, sequence: int, batch_id: str) -> None:
        self.operations.append("manifest_checkpoint")
        super().checkpoint(sequence, batch_id)


class FailingCommitRepository(InMemoryMessageRepository):
    def __init__(self) -> None:
        super().__init__()
        self.commit_attempts = 0

    def commit_batch(self) -> None:
        self.commit_attempts += 1
        if self.commit_attempts == 2:
            raise RuntimeError("database commit failed")
        super().commit_batch()


class TrackingFetcher(FakeFetcher):
    def __init__(
        self,
        folders: Iterable[RemoteFolder] = (),
        messages: Mapping[str, Iterable[FakeMessage | RemoteMessageRef]] | None = None,
        eml_bytes: Mapping[tuple[str, int], bytes] | None = None,
        *,
        uidvalidities: Mapping[str, int] | None = None,
        transient_failures: Mapping[tuple[str, int], int] | None = None,
        permanent_failures: Iterable[tuple[str, int]] = (),
    ) -> None:
        super().__init__(
            folders,
            messages,
            eml_bytes,
            uidvalidities=uidvalidities,
            transient_failures=transient_failures,
            permanent_failures=permanent_failures,
        )
        self.full_downloads: list[int] = []
        self.header_downloads: list[int] = []

    def download_eml_bytes(self, raw_name: str, uid: int) -> bytes:
        self.full_downloads.append(uid)
        return super().download_eml_bytes(raw_name, uid)

    def download_eml_headers(self, raw_name: str, uid: int) -> bytes:
        self.header_downloads.append(uid)
        return super().download_eml_headers(raw_name, uid)


class FlagTrackingFetcher(TrackingFetcher):
    def __init__(
        self,
        *args: Any,
        condstore: bool = False,
        highest_modseq: int | None = None,
        delta_uids: Iterable[int] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.capabilities = frozenset({"CONDSTORE"}) if condstore else frozenset()
        self.highest_modseq = highest_modseq
        self.flag_calls: list[tuple[str, tuple[int, ...]]] = []
        self.flag_since_calls: list[tuple[str, int]] = []
        self.return_no_deltas = False
        self.delta_uids = None if delta_uids is None else set(delta_uids)

    def iter_flags(
        self,
        raw_name: str,
        uids: Iterable[int],
        *,
        cancel: CancelToken | None = None,
    ) -> Iterator[RemoteMessageRef]:
        requested = tuple(uids)
        self.flag_calls.append((raw_name, requested))
        yield from super().iter_flags(raw_name, requested, cancel=cancel)

    def iter_flags_since(
        self,
        raw_name: str,
        modseq: int,
        *,
        cancel: CancelToken | None = None,
    ) -> Iterator[RemoteMessageRef]:
        self.flag_since_calls.append((raw_name, modseq))
        if self.return_no_deltas:
            return
        if self.delta_uids is None:
            yield from super().iter_flags_since(raw_name, modseq, cancel=cancel)
            return
        for uid in sorted(self.delta_uids):
            message = self.messages.get((raw_name, uid))
            if message is not None:
                yield RemoteMessageRef(uid=uid, flags=message.ref.flags)

    def get_highest_modseq(self) -> int | None:
        return self.highest_modseq


class FlagFailureFetcher(FlagTrackingFetcher):
    def iter_flags(
        self,
        raw_name: str,
        uids: Iterable[int],
        *,
        cancel: CancelToken | None = None,
    ) -> Iterator[RemoteMessageRef]:
        del raw_name, uids, cancel
        raise PermanentError("flag refresh failed")


class FlagAuthenticationFailureFetcher(FlagTrackingFetcher):
    def iter_flags(
        self,
        raw_name: str,
        uids: Iterable[int],
        *,
        cancel: CancelToken | None = None,
    ) -> Iterator[RemoteMessageRef]:
        del raw_name, uids, cancel
        raise AuthenticationError("flag credentials rejected")


class FlagCancellationFetcher(FlagTrackingFetcher):
    cancel_on_since = False

    def iter_flags_since(
        self,
        raw_name: str,
        modseq: int,
        *,
        cancel: CancelToken | None = None,
    ) -> Iterator[RemoteMessageRef]:
        if self.cancel_on_since and cancel is not None:
            cancel.cancel()
        yield from super().iter_flags_since(raw_name, modseq, cancel=cancel)


class AuthenticationFailureFetcher(FakeFetcher):
    def download_eml_bytes(self, raw_name: str, uid: int) -> bytes:
        del raw_name, uid
        raise AuthenticationError("credentials rejected")


def _eml(uid: int) -> bytes:
    return (
        "From: sender@example.com\r\n"
        f"Subject: Message {uid}\r\n"
        f"Message-ID: <message-{uid}@example.com>\r\n"
        "Date: Wed, 30 Jul 2026 12:00:00 +0000\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        f"Body {uid}\r\n"
    ).encode()


def _repository() -> tuple[InMemoryMessageRepository, int]:
    repo = InMemoryMessageRepository()
    repo.upsert_account({"id": "account", "provider_type": "imap"})
    folder_id = repo.upsert_folder(
        {
            "account_id": "account",
            "raw_name": "INBOX",
            "display_name": "Inbox",
            "is_sync_target": 1,
        }
    )
    return repo, folder_id


def _seed_flag_refresh_message(
    repo: InMemoryMessageRepository,
    folder_id: int,
    *,
    uid: int = 1,
    flags_seen_at: str = "2026-01-01T00:00:00Z",
) -> None:
    message = repo.get_message_by_uid("account", folder_id, 41, uid)
    assert message is not None
    repo.messages[int(message["id"])]["flags_seen_at"] = flags_seen_at


def test_initial_sync_processes_newest_first_and_initializes_cursors() -> None:
    repo, folder_id = _repository()
    refs = [
        RemoteMessageRef(
            uid=uid,
            internal_date=datetime(2026, 7, 30, tzinfo=UTC),
            size_bytes=len(_eml(uid)),
        )
        for uid in (1, 2, 3)
    ]
    fetcher = FakeFetcher(
        folders=[RemoteFolder("INBOX", "Inbox", uidvalidity=41)],
        messages={"INBOX": refs},
        eml_bytes={("INBOX", uid): _eml(uid) for uid in (1, 2, 3)},
    )
    storage = MemoryStorage()
    manifest = MemoryManifest()

    result = sync_account(
        fetcher,
        repo,
        storage,
        manifest,
        account_id="account",
        options=SyncOptions(),
        cancel=CancelToken(),
    )

    assert result.fetched_count == 3
    assert [event["uid"] for event in manifest.events if event["event"] == "fetch"] == [
        3,
        2,
        1,
    ]
    assert repo.cursors[folder_id] == {
        "uidvalidity": 41,
        "last_seen_uid": 3,
        "backfill_next_uid": 0,
        "initial_sync_completed": 1,
    }
    assert len(repo.messages) == 3
    assert manifest.sync_count >= 1


def test_failure_review_lists_exhausted_failures_with_message_metadata() -> None:
    repo, folder_id = _repository()
    repo.add_message(
        {
            "account_id": "account",
            "folder_id": folder_id,
            "uid": 7,
            "uidvalidity": 41,
            "subject": "oversized subject",
            "size_bytes": 60 * 1024 * 1024,
        }
    )
    for _ in range(10):
        repo.record_failure("account", folder_id, 41, 7, "oversize", "too large")

    failures = repo.list_failures_for_review()

    assert len(failures) == 1
    assert failures[0]["error_class"] == "oversize"
    assert failures[0]["attempt_count"] == 10
    assert failures[0]["subject"] == "oversized subject"
    assert failures[0]["message_id"] == 1
    assert failures[0]["folder_raw_name"] == "INBOX"


def test_force_fetch_message_ignores_size_limit_and_clears_failure() -> None:
    repo, folder_id = _repository()
    raw = _eml(7)
    repo.record_failure("account", folder_id, 41, 7, "oversize", "too large")
    fetcher = TrackingFetcher(
        folders=[RemoteFolder("INBOX", "Inbox", uidvalidity=41)],
        messages={
            "INBOX": [
                RemoteMessageRef(
                    uid=7,
                    internal_date=datetime(2026, 7, 30, tzinfo=UTC),
                    size_bytes=60,
                )
            ]
        },
        eml_bytes={("INBOX", 7): raw},
    )
    storage = MemoryStorage()
    manifest = MemoryManifest()

    result = force_fetch_message(
        fetcher,
        repo,
        storage,
        manifest,
        account_id="account",
        folder_raw_name="INBOX",
        folder_id=folder_id,
        uidvalidity=41,
        uid=7,
    )

    assert result == SyncResult(1, len(raw), 0, 0, False)
    assert fetcher.full_downloads == [7]
    assert repo.failures == {}
    message = repo.get_message_by_uid("account", folder_id, 41, 7)
    assert message is not None
    assert message["relative_path"] is not None
    assert [event["event"] for event in manifest.events] == ["fetch", "checkpoint"]


def test_manifest_checkpoint_follows_db_commit() -> None:
    tracked_repo = TrackingRepository()
    tracked_repo.upsert_account({"id": "account", "provider_type": "imap"})
    tracked_repo.upsert_folder(
        {
            "account_id": "account",
            "raw_name": "INBOX",
            "display_name": "Inbox",
            "is_sync_target": 1,
        }
    )
    raw = _eml(1)
    fetcher = FakeFetcher(
        folders=[RemoteFolder("INBOX", "Inbox", uidvalidity=41)],
        messages={"INBOX": [RemoteMessageRef(uid=1, size_bytes=len(raw))]},
        eml_bytes={("INBOX", 1): raw},
    )
    manifest = TrackingManifest(tracked_repo.operations)

    sync_account(
        fetcher,
        tracked_repo,
        MemoryStorage(),
        manifest,
        account_id="account",
        options=SyncOptions(),
        cancel=CancelToken(),
    )

    assert tracked_repo.operations[-2:] == ["db_commit", "manifest_checkpoint"]


def test_wal_checkpoint_runs_after_every_ten_message_batches() -> None:
    repo, _ = _repository()
    raw_by_uid = {uid: _eml(uid) for uid in range(1, 1002)}
    fetcher = FakeFetcher(
        folders=[RemoteFolder("INBOX", "Inbox", uidvalidity=41)],
        messages={
            "INBOX": [
                RemoteMessageRef(uid=uid, size_bytes=len(raw)) for uid, raw in raw_by_uid.items()
            ]
        },
        eml_bytes={("INBOX", uid): raw for uid, raw in raw_by_uid.items()},
    )
    manifest = MemoryManifest()

    sync_account(
        fetcher,
        repo,
        MemoryStorage(),
        manifest,
        account_id="account",
        options=SyncOptions(),
        cancel=CancelToken(),
    )

    checkpoints = [event for event in manifest.events if event["event"] == "checkpoint"]
    assert [event["sequence"] for event in checkpoints] == list(range(1, 12))
    assert repo.checkpoint_count == 1


def test_failed_db_commit_does_not_write_manifest_checkpoint() -> None:
    repo = FailingCommitRepository()
    repo.upsert_account({"id": "account", "provider_type": "imap"})
    repo.upsert_folder(
        {
            "account_id": "account",
            "raw_name": "INBOX",
            "display_name": "Inbox",
            "is_sync_target": 1,
        }
    )
    raw = _eml(1)
    fetcher = FakeFetcher(
        folders=[RemoteFolder("INBOX", "Inbox", uidvalidity=41)],
        messages={"INBOX": [RemoteMessageRef(uid=1, size_bytes=len(raw))]},
        eml_bytes={("INBOX", 1): raw},
    )
    manifest = MemoryManifest()

    with pytest.raises(RuntimeError, match="database commit failed"):
        sync_account(
            fetcher,
            repo,
            MemoryStorage(),
            manifest,
            account_id="account",
            options=SyncOptions(),
            cancel=CancelToken(),
        )

    assert [event for event in manifest.events if event["event"] == "checkpoint"] == []


def test_cancel_at_history_batch_boundary_resumes_without_duplicates() -> None:
    repo, folder_id = _repository()
    raw_by_uid = {uid: _eml(uid) for uid in range(1, 102)}
    fetcher = FakeFetcher(
        folders=[RemoteFolder("INBOX", "Inbox", uidvalidity=41)],
        messages={
            "INBOX": [
                RemoteMessageRef(uid=uid, size_bytes=len(raw)) for uid, raw in raw_by_uid.items()
            ]
        },
        eml_bytes={("INBOX", uid): raw for uid, raw in raw_by_uid.items()},
    )
    cancel = CancelToken()
    storage = MemoryStorage(
        on_save=lambda: cancel.cancel() if len(storage.save_calls) == 100 else None
    )
    manifest = MemoryManifest()

    first = sync_account(
        fetcher,
        repo,
        storage,
        manifest,
        account_id="account",
        options=SyncOptions(),
        cancel=cancel,
    )

    assert first.cancelled is True
    assert repo.cursors[folder_id]["backfill_next_uid"] == 1
    assert len(repo.messages) == 100

    second = sync_account(
        fetcher,
        repo,
        storage,
        manifest,
        account_id="account",
        options=SyncOptions(),
        cancel=CancelToken(),
    )

    assert second.cancelled is False
    assert repo.cursors[folder_id]["backfill_next_uid"] == 0
    assert len(repo.messages) == 101
    assert repo.commit_batch_count == 3


def test_oversize_downloads_headers_only_and_records_failure() -> None:
    repo, folder_id = _repository()
    raw = _eml(1)
    fetcher = TrackingFetcher(
        folders=[RemoteFolder("INBOX", "Inbox", uidvalidity=41)],
        messages={"INBOX": [RemoteMessageRef(uid=1, size_bytes=len(raw) + 100)]},
        eml_bytes={("INBOX", 1): raw},
    )

    result = sync_account(
        fetcher,
        repo,
        MemoryStorage(),
        MemoryManifest(),
        account_id="account",
        options=SyncOptions(max_message_bytes=10),
        cancel=CancelToken(),
    )

    message = repo.get_message_by_uid("account", folder_id, 41, 1)
    assert result.skipped_count == 1
    assert fetcher.full_downloads == []
    assert fetcher.header_downloads == [1]
    assert message is not None
    assert message["relative_path"] is None
    assert message["file_hash"] is None
    assert repo.contents == {}
    assert repo.failures[("account", folder_id, 41, 1)]["error_class"] == "oversize"


def test_parse_failure_keeps_eml_and_records_empty_contents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mail_dock.usecases.sync_mail as sync_module

    repo, folder_id = _repository()
    raw = _eml(1)
    fetcher = FakeFetcher(
        folders=[RemoteFolder("INBOX", "Inbox", uidvalidity=41)],
        messages={"INBOX": [RemoteMessageRef(uid=1, size_bytes=len(raw))]},
        eml_bytes={("INBOX", 1): raw},
    )

    def failed_parser(raw_bytes: bytes, internal_date: datetime | None) -> ParsedMessage:
        del raw_bytes, internal_date
        return ParsedMessage(parse_error="invalid MIME")

    monkeypatch.setattr(sync_module, "parse_eml", failed_parser)
    sync_account(
        fetcher,
        repo,
        MemoryStorage(),
        MemoryManifest(),
        account_id="account",
        options=SyncOptions(),
        cancel=CancelToken(),
    )

    message = repo.get_message_by_uid("account", folder_id, 41, 1)
    assert message is not None
    assert message["relative_path"] is not None
    assert repo.contents[message["id"]]["body_text"] == ""
    assert repo.failures[("account", folder_id, 41, 1)]["error_class"] == "parse"


def test_uidvalidity_change_keeps_old_generation_and_reuses_eml() -> None:
    repo, folder_id = _repository()
    raw = _eml(1)
    first_fetcher = FakeFetcher(
        folders=[RemoteFolder("INBOX", "Inbox", uidvalidity=41)],
        messages={"INBOX": [RemoteMessageRef(uid=1, size_bytes=len(raw))]},
        eml_bytes={("INBOX", 1): raw},
    )
    storage = MemoryStorage()
    manifest = MemoryManifest()
    sync_account(
        first_fetcher,
        repo,
        storage,
        manifest,
        account_id="account",
        options=SyncOptions(),
        cancel=CancelToken(),
    )

    second_fetcher = FakeFetcher(
        folders=[RemoteFolder("INBOX", "Inbox", uidvalidity=42)],
        messages={"INBOX": [RemoteMessageRef(uid=1, size_bytes=len(raw))]},
        eml_bytes={("INBOX", 1): raw},
    )
    sync_account(
        second_fetcher,
        repo,
        storage,
        manifest,
        account_id="account",
        options=SyncOptions(),
        cancel=CancelToken(),
    )

    assert repo.local_uids("account", folder_id, 41) == {1}
    assert repo.local_uids("account", folder_id, 42) == {1}
    assert len(repo.messages) == 2
    assert len(storage.save_calls) == 1
    assert repo.cursors[folder_id]["uidvalidity"] == 42


def test_new_mail_arriving_during_sync_waits_for_the_next_fixed_range() -> None:
    repo, folder_id = _repository()
    first_raw = _eml(1)
    fetcher = FakeFetcher(
        folders=[RemoteFolder("INBOX", "Inbox", uidvalidity=41)],
        messages={"INBOX": [RemoteMessageRef(uid=1, size_bytes=len(first_raw))]},
        eml_bytes={("INBOX", 1): first_raw},
    )
    storage = MemoryStorage()
    manifest = MemoryManifest()
    sync_account(
        fetcher,
        repo,
        storage,
        manifest,
        account_id="account",
        options=SyncOptions(),
        cancel=CancelToken(),
    )

    cancel = CancelToken()
    second_raw = _eml(2)
    third_raw = _eml(3)

    def add_mail_and_cancel() -> None:
        if len(storage.save_calls) == 2:
            fetcher.add_message(
                "INBOX", 3, third_raw, size_bytes=len(third_raw), message_id="<message-3>"
            )
            cancel.cancel()

    storage.on_save = add_mail_and_cancel
    fetcher.add_message(
        "INBOX", 2, second_raw, size_bytes=len(second_raw), message_id="<message-2>"
    )
    cancelled = sync_account(
        fetcher,
        repo,
        storage,
        manifest,
        account_id="account",
        options=SyncOptions(),
        cancel=cancel,
    )

    assert cancelled.cancelled is True
    assert repo.cursors[folder_id]["last_seen_uid"] == 2

    storage.on_save = None
    resumed = sync_account(
        fetcher,
        repo,
        storage,
        manifest,
        account_id="account",
        options=SyncOptions(),
        cancel=CancelToken(),
    )

    assert resumed.cancelled is False
    assert repo.cursors[folder_id]["last_seen_uid"] == 3
    assert len(repo.messages) == 3


def test_permanent_message_failure_does_not_stop_other_messages() -> None:
    repo, folder_id = _repository()
    raw_by_uid = {uid: _eml(uid) for uid in (1, 2)}
    fetcher = FakeFetcher(
        folders=[RemoteFolder("INBOX", "Inbox", uidvalidity=41)],
        messages={
            "INBOX": [
                RemoteMessageRef(uid=uid, size_bytes=len(raw)) for uid, raw in raw_by_uid.items()
            ]
        },
        eml_bytes={("INBOX", uid): raw for uid, raw in raw_by_uid.items()},
        permanent_failures={("INBOX", 2)},
    )

    result = sync_account(
        fetcher,
        repo,
        MemoryStorage(),
        MemoryManifest(),
        account_id="account",
        options=SyncOptions(),
        cancel=CancelToken(),
    )

    assert result.failed_count == 1
    assert repo.get_message_by_uid("account", folder_id, 41, 1) is not None
    assert repo.get_message_by_uid("account", folder_id, 41, 2) is None
    assert repo.failures[("account", folder_id, 41, 2)]["error_class"] == "permanent"


@pytest.mark.parametrize(
    ("candidate_count", "expected_state", "expected_event"),
    (
        (0, "deleted", "delete_detected"),
        (1, "moved", "moved"),
        (2, "unknown", "remote_state_unknown"),
    ),
)
def test_missing_uid_is_classified_without_deleting_eml(
    candidate_count: int,
    expected_state: str,
    expected_event: str,
) -> None:
    repo = InMemoryMessageRepository()
    repo.upsert_account({"id": "account", "provider_type": "imap"})
    folder_names = ["Source", *[f"Destination {index}" for index in range(candidate_count)]]
    folder_ids = {
        name: repo.upsert_folder(
            {
                "account_id": "account",
                "raw_name": name,
                "display_name": name,
                "is_sync_target": 1,
            }
        )
        for name in folder_names
    }
    raw = _eml(1)
    ref = RemoteMessageRef(uid=1, size_bytes=len(raw))
    fetcher = FakeFetcher(
        folders=[RemoteFolder(name, name, uidvalidity=41) for name in folder_names],
        messages={name: [ref] for name in folder_names},
        eml_bytes={(name, 1): raw for name in folder_names},
    )
    storage = MemoryStorage()
    manifest = MemoryManifest()

    sync_account(
        fetcher,
        repo,
        storage,
        manifest,
        account_id="account",
        options=SyncOptions(),
        cancel=CancelToken(),
    )
    fetcher.delete_remote_message("Source", 1)
    manifest.events.clear()

    sync_account(
        fetcher,
        repo,
        storage,
        manifest,
        account_id="account",
        options=SyncOptions(),
        cancel=CancelToken(),
    )

    source_message = repo.get_message_by_uid("account", folder_ids["Source"], 41, 1)
    assert source_message is not None
    assert source_message["remote_state"] == expected_state
    assert len(storage.raw_by_path) == 1
    assert [event["event"] for event in manifest.events] == [expected_event]
    if expected_state == "moved":
        assert source_message["moved_to_folder_id"] == folder_ids["Destination 0"]
    else:
        assert source_message["moved_to_folder_id"] is None


def test_authentication_error_aborts_sync() -> None:
    repo, _ = _repository()
    fetcher = AuthenticationFailureFetcher(
        folders=[RemoteFolder("INBOX", "Inbox", uidvalidity=41)],
        messages={"INBOX": [RemoteMessageRef(uid=1, size_bytes=len(_eml(1)))]},
        eml_bytes={("INBOX", 1): _eml(1)},
    )

    with pytest.raises(AuthenticationError, match="credentials rejected"):
        sync_account(
            fetcher,
            repo,
            MemoryStorage(),
            MemoryManifest(),
            account_id="account",
            options=SyncOptions(),
            cancel=CancelToken(),
        )


def _flag_fetcher(
    *, condstore: bool = False, highest_modseq: int | None = None
) -> FlagTrackingFetcher:
    raw = _eml(1)
    return FlagTrackingFetcher(
        folders=[RemoteFolder("INBOX", "Inbox", uidvalidity=41)],
        messages={
            "INBOX": [
                RemoteMessageRef(
                    uid=1,
                    internal_date=datetime(2026, 7, 30, tzinfo=UTC),
                    size_bytes=len(raw),
                    flags=(r"\Seen",),
                )
            ]
        },
        eml_bytes={("INBOX", 1): raw},
        condstore=condstore,
        highest_modseq=highest_modseq,
    )


def _complete_initial_sync(
    fetcher: FlagTrackingFetcher,
) -> tuple[InMemoryMessageRepository, int, MemoryStorage, MemoryManifest]:
    repo, folder_id = _repository()
    storage = MemoryStorage()
    manifest = MemoryManifest()
    sync_account(
        fetcher,
        repo,
        storage,
        manifest,
        account_id="account",
        options=SyncOptions(),
        cancel=CancelToken(),
    )
    return repo, folder_id, storage, manifest


def test_flag_refresh_uses_ttl_uid_fetch_and_updates_changed_flags() -> None:
    fetcher = _flag_fetcher()
    repo, folder_id, storage, manifest = _complete_initial_sync(fetcher)
    _seed_flag_refresh_message(repo, folder_id)
    raw = _eml(1)
    fetcher.add_message(
        "INBOX",
        1,
        raw,
        ref=RemoteMessageRef(
            uid=1,
            internal_date=datetime(2026, 7, 30, tzinfo=UTC),
            size_bytes=len(raw),
            flags=(r"\Seen", r"\Flagged"),
        ),
    )

    sync_account(
        fetcher,
        repo,
        storage,
        manifest,
        account_id="account",
        options=SyncOptions(),
        cancel=CancelToken(),
    )

    message = repo.get_message_by_uid("account", folder_id, 41, 1)
    assert message is not None
    assert message["imap_flags"] == r"\Seen \Flagged"
    assert fetcher.flag_calls == [("INBOX", (1,))]


def test_flag_refresh_does_not_touch_missing_non_condstore_response() -> None:
    class MissingFlagFetcher(FlagTrackingFetcher):
        def iter_flags(
            self,
            raw_name: str,
            uids: Iterable[int],
            *,
            cancel: CancelToken | None = None,
        ) -> Iterator[RemoteMessageRef]:
            self.flag_calls.append((raw_name, tuple(uids)))
            del cancel
            yield from ()

    fetcher = MissingFlagFetcher(
        folders=[RemoteFolder("INBOX", "Inbox", uidvalidity=41)],
        messages={
            "INBOX": [RemoteMessageRef(uid=1, internal_date=datetime(2026, 7, 30, tzinfo=UTC))]
        },
        eml_bytes={("INBOX", 1): _eml(1)},
    )
    repo, folder_id, storage, manifest = _complete_initial_sync(fetcher)
    _seed_flag_refresh_message(repo, folder_id)
    before = repo.get_message_by_uid("account", folder_id, 41, 1)
    assert before is not None
    before_seen_at = before["flags_seen_at"]

    sync_account(
        fetcher,
        repo,
        storage,
        manifest,
        account_id="account",
        options=SyncOptions(),
        cancel=CancelToken(),
    )

    after = repo.get_message_by_uid("account", folder_id, 41, 1)
    assert after is not None
    assert after["flags_seen_at"] == before_seen_at


def test_condstore_baseline_delta_and_empty_delta_touch_flags() -> None:
    fetcher = _flag_fetcher(condstore=True, highest_modseq=10)
    repo, folder_id, storage, manifest = _complete_initial_sync(fetcher)
    _seed_flag_refresh_message(repo, folder_id)

    sync_account(
        fetcher,
        repo,
        storage,
        manifest,
        account_id="account",
        options=SyncOptions(),
        cancel=CancelToken(),
    )
    assert fetcher.flag_calls[-1] == ("INBOX", (1,))
    assert repo.folders[folder_id]["highest_modseq"] == 10

    _seed_flag_refresh_message(repo, folder_id)
    fetcher.highest_modseq = 11
    fetcher.add_message(
        "INBOX",
        1,
        _eml(1),
        ref=RemoteMessageRef(
            uid=1,
            internal_date=datetime(2026, 7, 30, tzinfo=UTC),
            flags=(r"\Flagged",),
        ),
    )
    sync_account(
        fetcher,
        repo,
        storage,
        manifest,
        account_id="account",
        options=SyncOptions(),
        cancel=CancelToken(),
    )
    assert fetcher.flag_since_calls[-1] == ("INBOX", 10)
    assert repo.folders[folder_id]["highest_modseq"] == 11

    _seed_flag_refresh_message(repo, folder_id)
    fetcher.highest_modseq = 12
    fetcher.return_no_deltas = True
    sync_account(
        fetcher,
        repo,
        storage,
        manifest,
        account_id="account",
        options=SyncOptions(),
        cancel=CancelToken(),
    )
    assert fetcher.flag_since_calls[-1] == ("INBOX", 11)
    assert repo.folders[folder_id]["highest_modseq"] == 12
    message = repo.get_message_by_uid("account", folder_id, 41, 1)
    assert message is not None
    assert message["flags_seen_at"] != "2026-01-01T00:00:00Z"


def test_flag_refresh_fetch_error_isolated_from_account_sync() -> None:
    fetcher = FlagFailureFetcher(
        folders=[RemoteFolder("INBOX", "Inbox", uidvalidity=41)],
        messages={
            "INBOX": [RemoteMessageRef(uid=1, internal_date=datetime(2026, 7, 30, tzinfo=UTC))]
        },
        eml_bytes={("INBOX", 1): _eml(1)},
    )
    repo, folder_id, storage, manifest = _complete_initial_sync(fetcher)
    _seed_flag_refresh_message(repo, folder_id)

    result = sync_account(
        fetcher,
        repo,
        storage,
        manifest,
        account_id="account",
        options=SyncOptions(),
        cancel=CancelToken(),
    )

    assert result.cancelled is False
    assert repo.folders[folder_id].get("highest_modseq") is None


def test_flag_refresh_limits_window_and_fetches_only_expired_uids() -> None:
    recent = datetime(2026, 7, 30, tzinfo=UTC)
    old = datetime(2020, 1, 1, tzinfo=UTC)
    fetcher = FlagTrackingFetcher(
        folders=[RemoteFolder("INBOX", "Inbox", uidvalidity=41)],
        messages={
            "INBOX": [
                RemoteMessageRef(uid=1, internal_date=old, flags=(r"\Seen",)),
                RemoteMessageRef(uid=2, internal_date=recent, flags=(r"\Seen",)),
            ]
        },
        eml_bytes={("INBOX", 1): _eml(1), ("INBOX", 2): _eml(2)},
    )
    repo, folder_id, storage, manifest = _complete_initial_sync(fetcher)
    _seed_flag_refresh_message(repo, folder_id, uid=2)
    fetcher.add_message("INBOX", 1, _eml(1), ref=RemoteMessageRef(uid=1, flags=(r"\Flagged",)))
    fetcher.add_message("INBOX", 2, _eml(2), ref=RemoteMessageRef(uid=2, flags=(r"\Flagged",)))

    sync_account(
        fetcher,
        repo,
        storage,
        manifest,
        account_id="account",
        options=SyncOptions(),
        cancel=CancelToken(),
    )

    old_message = repo.get_message_by_uid("account", folder_id, 41, 1)
    recent_message = repo.get_message_by_uid("account", folder_id, 41, 2)
    assert old_message is not None and recent_message is not None
    assert old_message["imap_flags"] == r"\Seen"
    assert recent_message["imap_flags"] == r"\Flagged"
    assert fetcher.flag_calls == [("INBOX", (2,))]


def test_flag_refresh_skips_imap_when_no_uid_has_expired() -> None:
    fetcher = _flag_fetcher()
    repo, _, storage, manifest = _complete_initial_sync(fetcher)

    sync_account(
        fetcher,
        repo,
        storage,
        manifest,
        account_id="account",
        options=SyncOptions(),
        cancel=CancelToken(),
    )

    assert fetcher.flag_calls == []
    assert fetcher.flag_since_calls == []


def test_condstore_updates_fresh_delta_and_touches_expired_missing_delta() -> None:
    recent = datetime(2026, 7, 30, tzinfo=UTC)
    fetcher = FlagTrackingFetcher(
        folders=[RemoteFolder("INBOX", "Inbox", uidvalidity=41)],
        messages={
            "INBOX": [
                RemoteMessageRef(uid=1, internal_date=recent, flags=(r"\Seen",)),
                RemoteMessageRef(uid=2, internal_date=recent, flags=(r"\Seen",)),
            ]
        },
        eml_bytes={("INBOX", 1): _eml(1), ("INBOX", 2): _eml(2)},
        condstore=True,
        highest_modseq=10,
    )
    repo, folder_id, storage, manifest = _complete_initial_sync(fetcher)
    _seed_flag_refresh_message(repo, folder_id, uid=1)
    sync_account(
        fetcher,
        repo,
        storage,
        manifest,
        account_id="account",
        options=SyncOptions(),
        cancel=CancelToken(),
    )
    _seed_flag_refresh_message(repo, folder_id, uid=1)
    fetcher.delta_uids = {2}
    fetcher.highest_modseq = 11
    fetcher.add_message("INBOX", 2, _eml(2), ref=RemoteMessageRef(uid=2, flags=(r"\Flagged",)))

    sync_account(
        fetcher,
        repo,
        storage,
        manifest,
        account_id="account",
        options=SyncOptions(),
        cancel=CancelToken(),
    )

    expired_message = repo.get_message_by_uid("account", folder_id, 41, 1)
    fresh_message = repo.get_message_by_uid("account", folder_id, 41, 2)
    assert expired_message is not None and fresh_message is not None
    assert expired_message["imap_flags"] == r"\Seen"
    assert expired_message["flags_seen_at"] != "2026-01-01T00:00:00Z"
    assert fresh_message["imap_flags"] == r"\Flagged"
    assert fetcher.flag_since_calls[-1] == ("INBOX", 10)
    assert repo.folders[folder_id]["highest_modseq"] == 11


def test_condstore_nomodseq_uses_ttl_fallback_and_clears_saved_modseq() -> None:
    fetcher = _flag_fetcher(condstore=True, highest_modseq=10)
    repo, folder_id, storage, manifest = _complete_initial_sync(fetcher)
    _seed_flag_refresh_message(repo, folder_id)
    sync_account(
        fetcher,
        repo,
        storage,
        manifest,
        account_id="account",
        options=SyncOptions(),
        cancel=CancelToken(),
    )
    _seed_flag_refresh_message(repo, folder_id)
    fetcher.highest_modseq = None
    fetcher.add_message("INBOX", 1, _eml(1), ref=RemoteMessageRef(uid=1, flags=(r"\Flagged",)))

    sync_account(
        fetcher,
        repo,
        storage,
        manifest,
        account_id="account",
        options=SyncOptions(),
        cancel=CancelToken(),
    )

    message = repo.get_message_by_uid("account", folder_id, 41, 1)
    assert message is not None
    assert message["imap_flags"] == r"\Flagged"
    assert fetcher.flag_calls[-1] == ("INBOX", (1,))
    assert fetcher.flag_since_calls == []
    assert repo.folders[folder_id]["highest_modseq"] is None


def test_condstore_modseq_backtracking_rebuilds_baseline() -> None:
    fetcher = _flag_fetcher(condstore=True, highest_modseq=10)
    repo, folder_id, storage, manifest = _complete_initial_sync(fetcher)
    _seed_flag_refresh_message(repo, folder_id)
    sync_account(
        fetcher,
        repo,
        storage,
        manifest,
        account_id="account",
        options=SyncOptions(),
        cancel=CancelToken(),
    )
    _seed_flag_refresh_message(repo, folder_id)
    fetcher.highest_modseq = 9
    fetcher.add_message("INBOX", 1, _eml(1), ref=RemoteMessageRef(uid=1, flags=(r"\Flagged",)))

    sync_account(
        fetcher,
        repo,
        storage,
        manifest,
        account_id="account",
        options=SyncOptions(),
        cancel=CancelToken(),
    )

    assert fetcher.flag_calls[-1] == ("INBOX", (1,))
    assert fetcher.flag_since_calls == []
    assert repo.folders[folder_id]["highest_modseq"] == 9


def test_flag_refresh_cancellation_keeps_previous_modseq() -> None:
    fetcher = FlagCancellationFetcher(
        folders=[RemoteFolder("INBOX", "Inbox", uidvalidity=41)],
        messages={
            "INBOX": [
                RemoteMessageRef(
                    uid=1,
                    internal_date=datetime(2026, 7, 30, tzinfo=UTC),
                    flags=(r"\Seen",),
                )
            ]
        },
        eml_bytes={("INBOX", 1): _eml(1)},
        condstore=True,
        highest_modseq=10,
    )
    repo, folder_id, storage, manifest = _complete_initial_sync(fetcher)
    _seed_flag_refresh_message(repo, folder_id)
    sync_account(
        fetcher,
        repo,
        storage,
        manifest,
        account_id="account",
        options=SyncOptions(),
        cancel=CancelToken(),
    )
    _seed_flag_refresh_message(repo, folder_id)
    fetcher.highest_modseq = 11
    fetcher.cancel_on_since = True

    result = sync_account(
        fetcher,
        repo,
        storage,
        manifest,
        account_id="account",
        options=SyncOptions(),
        cancel=CancelToken(),
    )

    assert result.cancelled is True
    assert repo.folders[folder_id]["highest_modseq"] == 10


def test_flag_refresh_authentication_error_aborts_account_sync() -> None:
    fetcher = FlagAuthenticationFailureFetcher(
        folders=[RemoteFolder("INBOX", "Inbox", uidvalidity=41)],
        messages={
            "INBOX": [RemoteMessageRef(uid=1, internal_date=datetime(2026, 7, 30, tzinfo=UTC))]
        },
        eml_bytes={("INBOX", 1): _eml(1)},
    )
    repo, folder_id, storage, manifest = _complete_initial_sync(fetcher)
    _seed_flag_refresh_message(repo, folder_id)

    with pytest.raises(AuthenticationError, match="flag credentials rejected"):
        sync_account(
            fetcher,
            repo,
            storage,
            manifest,
            account_id="account",
            options=SyncOptions(),
            cancel=CancelToken(),
        )
