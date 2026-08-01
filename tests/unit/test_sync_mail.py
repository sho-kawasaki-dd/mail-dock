from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime

import pytest

from mail_dock.domain.errors import AuthenticationError, StorageError
from mail_dock.domain.fetcher import CancelToken, RemoteFolder, RemoteMessageRef
from mail_dock.domain.messages import ParsedMessage, StoredEml
from mail_dock.domain.ports import BaseEmlStorage, BaseManifestWriter, JSONValue
from mail_dock.usecases.sync_mail import SyncOptions, sync_account
from tests.support.fake_fetcher import FakeFetcher, FakeMessage
from tests.support.in_memory_repository import InMemoryMessageRepository


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
