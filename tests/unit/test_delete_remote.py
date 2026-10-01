from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from mail_dock.domain.errors import (
    FetchError,
    PermanentError,
    StorageDetachedError,
    StorageError,
    TransientError,
)
from mail_dock.domain.fetcher import CancelToken, RemoteFolder, RemoteMessageRef, RemoteMoveResult
from mail_dock.domain.imap_flags import has_imap_flag
from mail_dock.domain.message_identity import imap_source_item_key
from mail_dock.domain.messages import StoredEml
from mail_dock.domain.ports import BaseEmlStorage, BaseManifestReader, BaseManifestWriter, JSONValue
from mail_dock.domain.search import MessageSummary
from mail_dock.domain.storage_state import StorageState, StorageStateMachine
from mail_dock.usecases.delete_remote import (
    DeleteCandidate,
    DeleteScope,
    dry_run,
    execute,
    reconcile_uncertain_deletes,
    select_delete_scope,
)
from tests.support.fake_fetcher import FakeFetcher
from tests.support.in_memory_repository import InMemoryMessageRepository


@pytest.mark.parametrize(
    ("flags", "flag", "expected"),
    [
        (r"\Seen \Flagged", r"\flagged", True),
        (r"\FlaggedSomething", r"\Flagged", False),
        ("custom Flagged", r"\Flagged", False),
        (None, r"\Flagged", False),
    ],
)
def test_has_imap_flag_uses_case_insensitive_tokens(
    flags: str | None, flag: str, expected: bool
) -> None:
    assert has_imap_flag(flags, flag) is expected


class MemoryStorage(BaseEmlStorage):
    def __init__(self, files: Mapping[str, bytes]) -> None:
        self.files = dict(files)
        self.verified_reads: list[str] = []

    def save(self, account_id: str, internal_date: Any, raw: bytes) -> StoredEml:
        del account_id, internal_date
        path = "eml/account/message.eml"
        self.files[path] = raw
        return StoredEml(path, hashlib.sha256(raw).hexdigest(), len(raw))

    def reuse(self, relative_path: str, expected_hash: str) -> StoredEml | None:
        raw = self.files.get(relative_path)
        if raw is None or hashlib.sha256(raw).hexdigest() != expected_hash:
            return None
        return StoredEml(relative_path, expected_hash, len(raw), True)

    def read(self, relative_path: str) -> bytes:
        return self.files[relative_path]

    def read_verified(self, relative_path: str, expected_hash: str) -> bytes:
        self.verified_reads.append(relative_path)
        raw = self.files.get(relative_path)
        if raw is None:
            raise FileNotFoundError(relative_path)
        if hashlib.sha256(raw).hexdigest() != expected_hash:
            raise StorageError("EML file hash does not match expected hash")
        return raw


class MemoryManifest(BaseManifestWriter, BaseManifestReader):
    def __init__(self) -> None:
        self.events: list[dict[str, JSONValue]] = []
        self.flush_count = 0

    def append(self, event: Mapping[str, JSONValue]) -> None:
        self.events.append(dict(event))

    def flush_and_sync(self) -> None:
        self.flush_count += 1

    def checkpoint(self, sequence: int, batch_id: str) -> None:
        self.append(
            {
                "event": "checkpoint",
                "account_id": "account",
                "timestamp": "2026-08-27T00:00:00+00:00",
                "sequence": sequence,
                "batch_id": batch_id,
            }
        )
        self.flush_and_sync()

    def read_all_events(self) -> Iterator[Mapping[str, JSONValue]]:
        yield from self.events

    def read_last_checkpoint(self) -> Mapping[str, JSONValue] | None:
        checkpoints = [event for event in self.events if event.get("event") == "checkpoint"]
        return checkpoints[-1] if checkpoints else None

    def read_events_since_checkpoint(self) -> Iterator[Mapping[str, JSONValue]]:
        last_checkpoint = max(
            (
                index
                for index, event in enumerate(self.events)
                if event.get("event") == "checkpoint"
            ),
            default=-1,
        )
        yield from self.events[last_checkpoint + 1 :]

    def read_incomplete_intents(self) -> Iterator[Mapping[str, JSONValue]]:
        completed = {
            (
                event.get("account_id"),
                event.get("folder_raw_name"),
                event.get("uid"),
                event.get("uidvalidity"),
                event.get("mode"),
            )
            for event in self.events
            if event.get("event") == "remote_delete_completed"
        }
        for event in self.events:
            key = (
                event.get("account_id"),
                event.get("folder_raw_name"),
                event.get("uid"),
                event.get("uidvalidity"),
                event.get("mode"),
            )
            if event.get("event") == "remote_delete_intent" and key not in completed:
                yield event


class DeleteFetcher(FakeFetcher):
    def __init__(
        self,
        *,
        transient: bool = False,
        uidplus: bool = False,
        include_trash: bool = True,
    ) -> None:
        folders = [RemoteFolder("INBOX", "INBOX", 42)]
        if include_trash:
            folders.append(RemoteFolder("Trash", "Trash", 7, frozenset({r"\Trash"})))
        super().__init__(folders=folders)
        self.transient = transient
        self.uidplus = uidplus
        self.calls: list[tuple[str, int, str]] = []
        self.flag_calls: list[tuple[str, tuple[int, ...], int | None]] = []
        self.operation_order: list[str] = []
        self.select_uidvalidity_overrides: list[int] = []
        self.flag_error: FetchError | None = None
        self.flag_errors_by_folder: dict[str, FetchError] = {}

    def select_folder(self, raw_name: str) -> int:
        current = super().select_folder(raw_name)
        if self.select_uidvalidity_overrides:
            return self.select_uidvalidity_overrides.pop(0)
        return current

    def iter_flags(
        self,
        raw_name: str,
        uids: Iterable[int],
        *,
        expected_uidvalidity: int | None = None,
        cancel: CancelToken | None = None,
    ) -> Iterator[RemoteMessageRef]:
        requested = tuple(uids)
        self.flag_calls.append((raw_name, requested, expected_uidvalidity))
        self.operation_order.append(f"flags:{raw_name}")
        failure = self.flag_errors_by_folder.get(raw_name, self.flag_error)
        if failure is not None:
            raise failure
        yield from super().iter_flags(
            raw_name,
            requested,
            expected_uidvalidity=expected_uidvalidity,
            cancel=cancel,
        )

    def supports_uid_expunge(self) -> bool:
        return self.uidplus

    def move_remote_message_to_trash(
        self, raw_name: str, uid: int, *, expected_uidvalidity: int | None = None
    ) -> RemoteMoveResult | None:
        self.operation_order.append(f"delete:{raw_name}")
        self.calls.append((raw_name, uid, "trash"))
        if self.transient:
            raise TransientError("connection dropped after command was sent")
        return super().move_remote_message_to_trash(
            raw_name, uid, expected_uidvalidity=expected_uidvalidity
        )

    def expunge_remote_message(
        self, raw_name: str, uid: int, *, expected_uidvalidity: int | None = None
    ) -> None:
        self.calls.append((raw_name, uid, "expunge"))
        if self.transient:
            raise TransientError("connection dropped after command was sent")
        super().expunge_remote_message(raw_name, uid, expected_uidvalidity=expected_uidvalidity)

    def remove_remote_membership(
        self, raw_name: str, uid: int, *, expected_uidvalidity: int | None = None
    ) -> None:
        self.calls.append((raw_name, uid, "remove_membership"))
        if self.transient:
            raise TransientError("connection dropped after command was sent")
        super().remove_remote_membership(
            raw_name, uid, expected_uidvalidity=expected_uidvalidity
        )


def _record(
    repository: InMemoryMessageRepository,
    *,
    message_id: int,
    raw: bytes,
    imap_flags: str | None = None,
) -> str:
    repository.upsert_account({"id": "account"})
    folder_id = repository.upsert_folder(
        {"account_id": "account", "raw_name": "INBOX", "uidvalidity": 42}
    )
    path = f"eml/account/{message_id}.eml"
    repository.add_message(
        {
            "id": message_id,
            "account_id": "account",
            "folder_id": folder_id,
            "uid": message_id,
            "uidvalidity": 42,
            "source_item_key": f"42:{message_id}",
            "remote_state": "present",
            "imap_flags": imap_flags,
            "local_state": "active",
            "relative_path": path,
            "file_hash": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
            "subject": f"Subject {message_id}",
            "date_sent": "2026-08-27T00:00:00+00:00",
            "internal_date": "2026-08-27T00:00:00+00:00",
        },
        {"subject": f"Subject {message_id}", "body_text": "body"},
    )
    return path


def _summary(
    message_id: int,
    *,
    date_sent: datetime | None = None,
    internal_date: datetime | None = None,
    remote_state: str = "present",
    imap_flags: str | None = None,
) -> MessageSummary:
    return MessageSummary(
        id=message_id,
        account_id="account",
        folder_id=1,
        folder_raw_name="INBOX",
        folder_display_name="INBOX",
        subject=f"Subject {message_id}",
        sender="sender@example.test",
        date_sent=date_sent,
        internal_date=internal_date,
        size_bytes=1,
        has_attachment=False,
        remote_state=remote_state,
        local_state="active",
        thread_key=None,
        imap_flags=imap_flags,
        moved_to_folder_display_name=None,
        failure_class=None,
        flags_seen_at=None,
    )


def test_select_delete_scope_excludes_states_before_flags_and_applies_stable_limit() -> None:
    same_date = datetime(2026, 1, 2, tzinfo=UTC)
    scope = select_delete_scope(
        (
            _summary(5, date_sent=same_date),
            _summary(2),
            _summary(1, date_sent=same_date),
            _summary(3, imap_flags=r"\Flagged"),
            _summary(4, remote_state="uncertain", imap_flags=r"\Flagged"),
        ),
        exclude_flagged=True,
        limit=2,
    )

    assert scope.message_ids == (2, 1)
    assert scope.flagged_message_ids == (3,)
    assert scope.non_deletable_message_ids == (4,)
    assert scope.matched_count == 5
    assert scope.flagged_excluded_count == 1
    assert scope.truncated
    assert scope.delete_batch_limit == 2


def test_select_delete_scope_off_still_excludes_non_present_and_rejects_bad_limit() -> None:
    scope = select_delete_scope(
        (_summary(1, imap_flags=r"\Flagged"), _summary(2, remote_state="moved")),
        exclude_flagged=False,
        limit=1,
    )

    assert scope.message_ids == (1,)
    assert scope.flagged_message_ids == ()
    assert scope.non_deletable_message_ids == (2,)
    with pytest.raises(ValueError, match="limit must be positive"):
        select_delete_scope((), exclude_flagged=False, limit=0)


def test_dry_run_keeps_scope_exclusions_fixed_and_skips_eml_checks_for_exclusions() -> None:
    repository = InMemoryMessageRepository()
    raw = b"message"
    paths = [_record(repository, message_id=message_id, raw=raw) for message_id in (1, 2, 3)]
    storage = MemoryStorage(dict.fromkeys(paths, raw))
    state = StorageStateMachine(StorageState.ATTACHED)
    scope = DeleteScope(
        message_ids=(1,),
        flagged_message_ids=(2,),
        non_deletable_message_ids=(3,),
        matched_count=3,
        flagged_excluded_count=1,
        truncated=False,
        delete_batch_limit=1,
    )
    repository.messages[2]["imap_flags"] = None
    repository.messages[3]["remote_state"] = "present"

    result = dry_run(
        repository,
        storage,
        message_ids=(1, 2, 3),
        storage_state=state,
        folder_id=1,
        exclude_flagged=True,
        scope=scope,
    )

    assert [candidate.message_id for candidate in result.candidates] == [1]
    assert {item.message_id: item.reason for item in result.exclusions} == {
        2: "flagged",
        3: "remote_state_not_deletable",
    }
    assert storage.verified_reads == [paths[0]]
    assert result.exclude_flagged is True
    assert result.scope == scope


def test_dry_run_does_not_exclude_flagged_messages_when_option_is_off() -> None:
    repository = InMemoryMessageRepository()
    raw = b"message"
    path = _record(repository, message_id=1, raw=raw, imap_flags=r"\Flagged")
    storage = MemoryStorage({path: raw})
    state = StorageStateMachine(StorageState.ATTACHED)

    result = dry_run(
        repository,
        storage,
        message_ids=(1,),
        storage_state=state,
        exclude_flagged=False,
    )

    assert [candidate.message_id for candidate in result.candidates] == [1]
    assert result.exclusions == ()


def test_dry_run_flag_and_changed_scope_candidate_are_excluded_before_eml_read() -> None:
    repository = InMemoryMessageRepository()
    raw = b"message"
    path = _record(repository, message_id=1, raw=raw, imap_flags=r"\Flagged")
    storage = MemoryStorage({path: raw})
    state = StorageStateMachine(StorageState.ATTACHED)

    flagged = dry_run(
        repository,
        storage,
        message_ids=(1,),
        storage_state=state,
        exclude_flagged=True,
    )
    assert flagged.exclusions[0].reason == "flagged"
    assert storage.verified_reads == []

    repository.messages[1]["remote_state"] = "moved"
    scope = DeleteScope((1,), (), (), 1, 0, False, 1)
    changed = dry_run(
        repository,
        storage,
        message_ids=(1,),
        storage_state=state,
        scope=scope,
    )
    assert changed.exclusions[0].reason == "remote_state_not_deletable"
    assert storage.verified_reads == []


def test_execute_skips_server_flagged_message_without_recording_intent() -> None:
    repository = InMemoryMessageRepository()
    raw = b"message"
    path = _record(repository, message_id=1, raw=raw)
    storage = MemoryStorage({path: raw})
    state = StorageStateMachine(StorageState.ATTACHED)
    plan = dry_run(repository, storage, message_ids=(1,), storage_state=state)
    manifest = MemoryManifest()
    fetcher = DeleteFetcher()
    fetcher.add_message("INBOX", 1, raw, flags=(r"\Flagged",))

    result = execute(
        fetcher,
        repository,
        storage,
        manifest,
        plan=plan,
        storage_state=state,
        exclude_flagged=True,
    )

    assert result.skipped_ids == (1,)
    assert result.errors == ((1, "flagged_on_server"),)
    assert fetcher.calls == []
    assert fetcher.flag_calls == [("INBOX", (1,), 42)]
    assert manifest.events == []
    assert repository.messages[1]["imap_flags"] == r"\Flagged"


def test_execute_skips_flag_cache_update_when_candidate_has_no_folder_id() -> None:
    repository = InMemoryMessageRepository()
    raw = b"message"
    path = _record(repository, message_id=1, raw=raw)
    repository.messages[1]["source_item_key"] = None
    repository.messages[1]["folder_id"] = None
    repository.messages[1]["folder_raw_name"] = "INBOX"
    storage = MemoryStorage({path: raw})
    state = StorageStateMachine(StorageState.ATTACHED)
    plan = dry_run(repository, storage, message_ids=(1,), storage_state=state)
    fetcher = DeleteFetcher()
    fetcher.add_message("INBOX", 1, raw, flags=(r"\Seen",))

    result = execute(
        fetcher,
        repository,
        storage,
        MemoryManifest(),
        plan=plan,
        storage_state=state,
        exclude_flagged=True,
    )

    assert result.completed_ids == (1,)
    assert repository.messages[1]["imap_flags"] is None


def test_execute_missing_flags_response_is_unverified() -> None:
    repository = InMemoryMessageRepository()
    raw = b"message"
    path = _record(repository, message_id=1, raw=raw)
    storage = MemoryStorage({path: raw})
    state = StorageStateMachine(StorageState.ATTACHED)
    plan = dry_run(repository, storage, message_ids=(1,), storage_state=state)
    manifest = MemoryManifest()

    result = execute(
        DeleteFetcher(),
        repository,
        storage,
        manifest,
        plan=plan,
        storage_state=state,
        exclude_flagged=True,
    )

    assert result.errors == ((1, "flag_unverified"),)
    assert manifest.events == []


def test_execute_aborts_preflight_when_storage_detaches() -> None:
    class DetachedFlagFetcher(DeleteFetcher):
        def iter_flags(
            self,
            raw_name: str,
            uids: Iterable[int],
            *,
            expected_uidvalidity: int | None = None,
            cancel: CancelToken | None = None,
        ) -> Iterator[RemoteMessageRef]:
            del raw_name, uids, expected_uidvalidity, cancel
            raise StorageDetachedError("storage detached during flag check")

    repository = InMemoryMessageRepository()
    raw = b"message"
    path = _record(repository, message_id=1, raw=raw)
    storage = MemoryStorage({path: raw})
    state = StorageStateMachine(StorageState.ATTACHED)
    plan = dry_run(repository, storage, message_ids=(1,), storage_state=state)
    manifest = MemoryManifest()
    fetcher = DetachedFlagFetcher()

    with pytest.raises(StorageDetachedError):
        execute(
            fetcher,
            repository,
            storage,
            manifest,
            plan=plan,
            storage_state=state,
            exclude_flagged=True,
        )

    assert fetcher.calls == []
    assert manifest.events == []


def test_execute_uidvalidity_mismatch_before_iter_flags_skips_folder() -> None:
    repository = InMemoryMessageRepository()
    raw = b"message"
    path = _record(repository, message_id=1, raw=raw)
    storage = MemoryStorage({path: raw})
    state = StorageStateMachine(StorageState.ATTACHED)
    plan = dry_run(repository, storage, message_ids=(1,), storage_state=state)
    manifest = MemoryManifest()
    fetcher = DeleteFetcher()
    fetcher.select_uidvalidity_overrides = [43]

    result = execute(
        fetcher,
        repository,
        storage,
        manifest,
        plan=plan,
        storage_state=state,
        exclude_flagged=True,
    )

    assert result.errors == ((1, "uidvalidity_mismatch"),)
    assert fetcher.flag_calls == []
    assert fetcher.calls == []
    assert manifest.events == []


def test_execute_iter_flags_internal_select_mismatch_has_no_intent() -> None:
    repository = InMemoryMessageRepository()
    raw = b"message"
    path = _record(repository, message_id=1, raw=raw)
    storage = MemoryStorage({path: raw})
    state = StorageStateMachine(StorageState.ATTACHED)
    plan = dry_run(repository, storage, message_ids=(1,), storage_state=state)
    manifest = MemoryManifest()
    fetcher = DeleteFetcher()
    fetcher.add_message("INBOX", 1, raw)
    fetcher.select_uidvalidity_overrides = [42, 43]

    result = execute(
        fetcher,
        repository,
        storage,
        manifest,
        plan=plan,
        storage_state=state,
        exclude_flagged=True,
    )

    assert result.errors == ((1, "uidvalidity_mismatch"),)
    assert fetcher.flag_calls == [("INBOX", (1,), 42)]
    assert fetcher.calls == []
    assert manifest.events == []


@pytest.mark.parametrize(
    ("failure", "expected_reason"),
    [(TransientError("offline"), None), (PermanentError("bad FETCH"), "flag_unverified")],
)
def test_execute_handles_flag_confirmation_failures_before_any_intent(
    failure: FetchError, expected_reason: str | None
) -> None:
    repository = InMemoryMessageRepository()
    raw = b"message"
    path = _record(repository, message_id=1, raw=raw)
    storage = MemoryStorage({path: raw})
    state = StorageStateMachine(StorageState.ATTACHED)
    plan = dry_run(repository, storage, message_ids=(1,), storage_state=state)
    manifest = MemoryManifest()
    fetcher = DeleteFetcher()
    fetcher.flag_error = failure

    if expected_reason is None:
        with pytest.raises(TransientError):
            execute(
                fetcher,
                repository,
                storage,
                manifest,
                plan=plan,
                storage_state=state,
                exclude_flagged=True,
            )
        assert manifest.events == []
    else:
        result = execute(
            fetcher,
            repository,
            storage,
            manifest,
            plan=plan,
            storage_state=state,
            exclude_flagged=True,
        )
        assert result.errors == ((1, expected_reason),)
        assert manifest.events == []
    assert fetcher.calls == []


def test_execute_does_not_delete_until_every_folder_flag_check_finishes() -> None:
    repository = InMemoryMessageRepository()
    first_raw = b"first"
    second_raw = b"second"
    first_path = _record(repository, message_id=1, raw=first_raw)
    second_path = _record(repository, message_id=2, raw=second_raw)
    archive_id = repository.upsert_folder(
        {"account_id": "account", "raw_name": "Archive", "uidvalidity": 7}
    )
    archive_membership = dict(repository.list_message_memberships("account", "42:2")[0])
    archive_membership.update(
        {"folder_id": archive_id, "folder_raw_name": "Archive", "uidvalidity": 7}
    )
    repository.replace_message_memberships(2, [archive_membership])
    storage = MemoryStorage({first_path: first_raw, second_path: second_raw})
    state = StorageStateMachine(StorageState.ATTACHED)
    plan = dry_run(repository, storage, message_ids=(1, 2), storage_state=state)
    fetcher = DeleteFetcher()
    fetcher.add_folder(RemoteFolder("Archive", "Archive", 7))
    fetcher.add_message("INBOX", 1, first_raw)
    fetcher.add_message("Archive", 2, second_raw)

    result = execute(
        fetcher,
        repository,
        storage,
        MemoryManifest(),
        plan=plan,
        storage_state=state,
        exclude_flagged=True,
    )

    assert result.completed_ids == (1, 2)
    assert fetcher.operation_order == [
        "flags:INBOX",
        "flags:Archive",
        "delete:INBOX",
        "delete:Archive",
    ]


@pytest.mark.parametrize(
    ("failure", "should_abort"),
    [(TransientError("offline"), True), (PermanentError("bad FLAGS"), False)],
)
def test_later_folder_flag_failure_never_deletes_before_preflight_finishes(
    failure: FetchError, should_abort: bool
) -> None:
    repository = InMemoryMessageRepository()
    first_raw = b"first"
    second_raw = b"second"
    first_path = _record(repository, message_id=1, raw=first_raw)
    second_path = _record(repository, message_id=2, raw=second_raw)
    archive_id = repository.upsert_folder(
        {"account_id": "account", "raw_name": "Archive", "uidvalidity": 7}
    )
    archive_membership = dict(repository.list_message_memberships("account", "42:2")[0])
    archive_membership.update(
        {"folder_id": archive_id, "folder_raw_name": "Archive", "uidvalidity": 7}
    )
    repository.replace_message_memberships(2, [archive_membership])
    storage = MemoryStorage({first_path: first_raw, second_path: second_raw})
    state = StorageStateMachine(StorageState.ATTACHED)
    plan = dry_run(repository, storage, message_ids=(1, 2), storage_state=state)
    manifest = MemoryManifest()
    fetcher = DeleteFetcher()
    fetcher.add_folder(RemoteFolder("Archive", "Archive", 7))
    fetcher.add_message("INBOX", 1, first_raw)
    fetcher.add_message("Archive", 2, second_raw)
    fetcher.flag_errors_by_folder["Archive"] = failure

    if should_abort:
        with pytest.raises(TransientError):
            execute(
                fetcher,
                repository,
                storage,
                manifest,
                plan=plan,
                storage_state=state,
                exclude_flagged=True,
            )
        assert manifest.events == []
        assert fetcher.calls == []
    else:
        result = execute(
            fetcher,
            repository,
            storage,
            manifest,
            plan=plan,
            storage_state=state,
            exclude_flagged=True,
        )
        assert result.completed_ids == (1,)
        assert result.errors == ((2, "flag_unverified"),)
        assert fetcher.calls == [("INBOX", 1, "trash")]
    assert fetcher.operation_order[:2] == ["flags:INBOX", "flags:Archive"]


def test_delete_select_uidvalidity_change_leaves_intent_and_skips_same_folder_remainder() -> None:
    repository = InMemoryMessageRepository()
    raws = {message_id: f"message-{message_id}".encode() for message_id in (1, 2)}
    paths = {
        message_id: _record(repository, message_id=message_id, raw=raw)
        for message_id, raw in raws.items()
    }
    storage = MemoryStorage({paths[message_id]: raw for message_id, raw in raws.items()})
    state = StorageStateMachine(StorageState.ATTACHED)
    plan = dry_run(repository, storage, message_ids=(1, 2), storage_state=state)
    manifest = MemoryManifest()
    fetcher = DeleteFetcher()
    for message_id, raw in raws.items():
        fetcher.add_message("INBOX", message_id, raw)
    fetcher.select_uidvalidity_overrides = [42, 42, 43]

    result = execute(
        fetcher,
        repository,
        storage,
        manifest,
        plan=plan,
        storage_state=state,
        exclude_flagged=True,
    )

    assert result.errors == (
        (1, "uidvalidity_mismatch"),
        (2, "uidvalidity_mismatch"),
    )
    assert [event["event"] for event in manifest.events] == ["remote_delete_intent"]
    assert fetcher.list_existing_uids("INBOX") == {1, 2}


def test_dry_run_excludes_invalid_eml_and_missing_contents() -> None:
    repository = InMemoryMessageRepository()
    valid_raw = b"valid"
    valid_path = _record(repository, message_id=1, raw=valid_raw)
    _record(repository, message_id=2, raw=b"other")
    repository.messages[2]["file_hash"] = "0" * 64
    _record(repository, message_id=3, raw=b"no contents")
    del repository.contents[3]
    _record(repository, message_id=4, raw=b"missing file")
    storage = MemoryStorage(
        {
            valid_path: valid_raw,
            "eml/account/2.eml": b"other",
            "eml/account/3.eml": b"no contents",
            # message 4's EML is intentionally absent (precondition 1: file must exist)
        }
    )
    state = StorageStateMachine(StorageState.ATTACHED)

    result = dry_run(repository, storage, message_ids=(1, 2, 3, 4), storage_state=state)

    assert [candidate.message_id for candidate in result.candidates] == [1]
    assert result.total_size_bytes == len(valid_raw)
    assert {item.message_id: item.reason for item in result.exclusions} == {
        2: "hash_mismatch",
        3: "message_contents_missing",
        4: "eml_missing",
    }


def test_remote_delete_rejects_pst_archive_messages() -> None:
    repository = InMemoryMessageRepository()
    path = _record(repository, message_id=1, raw=b"pst message")
    repository.accounts["account"]["provider_type"] = "pst_import"
    storage = MemoryStorage({path: b"pst message"})
    state = StorageStateMachine(StorageState.ATTACHED)

    with pytest.raises(PermanentError, match="PST archive account"):
        dry_run(repository, storage, message_ids=(1,), storage_state=state)

    with pytest.raises(PermanentError, match="PST archive account"):
        execute(
            DeleteFetcher(),
            repository,
            storage,
            MemoryManifest(),
            plan=(
                DeleteCandidate(
                    message_id=1,
                    account_id="account",
                    folder_raw_name="INBOX",
                    uid=1,
                    uidvalidity=42,
                    subject="Subject 1",
                    date_sent="2026-08-27T00:00:00+00:00",
                    internal_date="2026-08-27T00:00:00+00:00",
                    size_bytes=len(b"pst message"),
                    relative_path=path,
                    file_hash=hashlib.sha256(b"pst message").hexdigest(),
                ),
            ),
            storage_state=state,
        )


def test_execute_records_intent_then_completion_and_updates_state() -> None:
    repository = InMemoryMessageRepository()
    raw = b"message"
    path = _record(repository, message_id=1, raw=raw)
    storage = MemoryStorage({path: raw})
    state = StorageStateMachine(StorageState.ATTACHED)
    plan = dry_run(repository, storage, message_ids=(1,), storage_state=state)
    manifest = MemoryManifest()
    fetcher = DeleteFetcher()
    fetcher.add_message("INBOX", 1, raw)

    result = execute(
        fetcher,
        repository,
        storage,
        manifest,
        plan=plan,
        mode="trash",
        storage_state=state,
    )

    assert result.completed_ids == (1,)
    assert [event["event"] for event in manifest.events] == [
        "remote_delete_intent",
        "remote_delete_completed",
    ]
    assert repository.messages[1]["remote_state"] == "deleted"
    assert repository.audit_log[0]["operation"] == "remote_delete"
    assert fetcher.calls == [("INBOX", 1, "trash")]
    assert fetcher.flag_calls == []
    assert manifest.flush_count == 2


def test_execute_rejects_detached_and_unsafe_expunge_before_imap() -> None:
    repository = InMemoryMessageRepository()
    raw = b"message"
    path = _record(repository, message_id=1, raw=raw)
    storage = MemoryStorage({path: raw})
    attached = StorageStateMachine(StorageState.ATTACHED)
    plan = dry_run(repository, storage, message_ids=(1,), storage_state=attached)
    fetcher = DeleteFetcher()
    manifest = MemoryManifest()

    with pytest.raises(StorageDetachedError):
        execute(
            fetcher,
            repository,
            storage,
            manifest,
            plan=plan,
            storage_state=StorageStateMachine(StorageState.DETACHED),
        )
    with pytest.raises(PermanentError):
        execute(
            fetcher,
            repository,
            storage,
            manifest,
            plan=plan,
            mode="expunge",
            storage_state=attached,
        )
    assert fetcher.calls == []
    assert manifest.events == []


def test_execute_rejects_gmail_expunge_even_with_uidplus() -> None:
    repository = InMemoryMessageRepository()
    raw = b"message"
    path = _record(repository, message_id=1, raw=raw)
    repository.accounts["account"].update({"auth_type": "xoauth2", "oauth_provider": "google"})
    storage = MemoryStorage({path: raw})
    state = StorageStateMachine(StorageState.ATTACHED)
    plan = dry_run(repository, storage, message_ids=(1,), storage_state=state)
    manifest = MemoryManifest()
    fetcher = DeleteFetcher(uidplus=True)

    with pytest.raises(PermanentError, match="Gmail accounts"):
        execute(
            fetcher,
            repository,
            storage,
            manifest,
            plan=plan,
            mode="expunge",
            storage_state=state,
        )

    assert fetcher.calls == []
    assert manifest.events == []


def test_gmail_membership_removal_is_folder_specific_and_snapshot_first() -> None:
    repository = InMemoryMessageRepository()
    raw = b"message"
    path = _record(repository, message_id=1, raw=raw)
    repository.accounts["account"].update({"auth_type": "xoauth2", "oauth_provider": "google"})
    second_folder_id = repository.upsert_folder(
        {
            "account_id": "account",
            "raw_name": "Important",
            "display_name": "Important",
            "uidvalidity": 7,
        }
    )
    first_membership = dict(repository.list_message_memberships("account", "42:1")[0])
    second_membership = {
        **first_membership,
        "folder_id": second_folder_id,
        "folder_raw_name": "Important",
        "uid": 9,
        "uidvalidity": 7,
    }
    repository.replace_message_memberships(1, [first_membership, second_membership])
    repository.messages[1]["source_item_key"] = "gmail:123456789"
    storage = MemoryStorage({path: raw})
    state = StorageStateMachine(StorageState.ATTACHED)
    ambiguous_plan = dry_run(repository, storage, message_ids=(1,), storage_state=state)

    assert ambiguous_plan.candidates == ()
    assert ambiguous_plan.exclusions[0].reason == "folder_selection_required"

    plan = dry_run(
        repository,
        storage,
        message_ids=(1,),
        storage_state=state,
        folder_id=second_folder_id,
    )
    fetcher = DeleteFetcher()
    fetcher.add_folder(RemoteFolder("Important", "Important", 7))
    fetcher.add_message("Important", 9, raw)
    manifest = MemoryManifest()

    result = execute(
        fetcher,
        repository,
        storage,
        manifest,
        plan=plan,
        mode="remove_membership",
        storage_state=state,
    )

    assert result.completed_ids == (1,)
    assert fetcher.calls == [("Important", 9, "remove_membership")]
    assert [event["event"] for event in manifest.events] == [
        "remote_delete_intent",
        "remote_delete_completed",
        "message_membership_snapshot",
    ]
    assert manifest.events[-1]["memberships"] == [
        {
            "folder_raw_name": "INBOX",
            "uid": 1,
            "uidvalidity": 42,
            "remote_state": "present",
            "moved_to_folder_raw_name": None,
            "imap_flags": None,
            "flags_seen_at": None,
            "last_seen_at": None,
        }
    ]
    assert [
        item["folder_id"]
        for item in repository.list_message_memberships("account", "gmail:123456789")
    ] == [first_membership["folder_id"]]


def test_transient_delete_is_recorded_as_uncertain_without_marking_deleted() -> None:
    repository = InMemoryMessageRepository()
    raw = b"message"
    path = _record(repository, message_id=1, raw=raw)
    storage = MemoryStorage({path: raw})
    state = StorageStateMachine(StorageState.ATTACHED)
    plan = dry_run(repository, storage, message_ids=(1,), storage_state=state)
    manifest = MemoryManifest()

    result = execute(
        DeleteFetcher(transient=True),
        repository,
        storage,
        manifest,
        plan=plan,
        storage_state=state,
    )

    assert result.uncertain_ids == (1,)
    assert repository.messages[1]["remote_state"] == "present"
    assert [event["event"] for event in manifest.events] == [
        "remote_delete_intent",
        "remote_delete_uncertain",
    ]


def test_execute_rejects_a_batch_larger_than_the_configured_limit() -> None:
    repository = InMemoryMessageRepository()
    raw = b"message"
    path_one = _record(repository, message_id=1, raw=raw)
    path_two = _record(repository, message_id=2, raw=raw)
    storage = MemoryStorage({path_one: raw, path_two: raw})
    state = StorageStateMachine(StorageState.ATTACHED)
    plan = dry_run(repository, storage, message_ids=(1, 2), storage_state=state)
    manifest = MemoryManifest()
    fetcher = DeleteFetcher()

    with pytest.raises(ValueError, match="batch limit"):
        execute(
            fetcher,
            repository,
            storage,
            manifest,
            plan=plan,
            storage_state=state,
            delete_batch_limit=1,
        )

    assert fetcher.calls == []
    assert manifest.events == []


def test_execute_defaults_to_trash_mode_when_unspecified() -> None:
    repository = InMemoryMessageRepository()
    raw = b"message"
    path = _record(repository, message_id=1, raw=raw)
    storage = MemoryStorage({path: raw})
    state = StorageStateMachine(StorageState.ATTACHED)
    plan = dry_run(repository, storage, message_ids=(1,), storage_state=state)
    manifest = MemoryManifest()
    fetcher = DeleteFetcher()
    fetcher.add_message("INBOX", 1, raw)

    result = execute(fetcher, repository, storage, manifest, plan=plan, storage_state=state)

    assert result.completed_ids == (1,)
    assert fetcher.calls == [("INBOX", 1, "trash")]


def test_execute_rejects_a_plan_whose_eml_was_replaced_after_dry_run() -> None:
    repository = InMemoryMessageRepository()
    raw = b"message"
    path = _record(repository, message_id=1, raw=raw)
    storage = MemoryStorage({path: raw})
    state = StorageStateMachine(StorageState.ATTACHED)
    plan = dry_run(repository, storage, message_ids=(1,), storage_state=state)
    manifest = MemoryManifest()
    fetcher = DeleteFetcher()
    fetcher.add_message("INBOX", 1, raw)

    # The on-disk EML changed after the dry run without the DB record being updated
    # (e.g. external corruption); the pre-execution re-check must catch this.
    storage.files[path] = b"tampered"

    result = execute(
        fetcher,
        repository,
        storage,
        manifest,
        plan=plan,
        storage_state=state,
    )

    assert result.completed_ids == ()
    assert result.skipped_ids == (1,)
    assert dict(result.errors)[1] == "hash_mismatch"
    assert fetcher.calls == []
    assert manifest.events == []
    assert repository.messages[1]["remote_state"] == "present"


def test_execute_rejects_trash_mode_when_the_trash_folder_is_unresolved() -> None:
    repository = InMemoryMessageRepository()
    raw = b"message"
    path = _record(repository, message_id=1, raw=raw)
    storage = MemoryStorage({path: raw})
    state = StorageStateMachine(StorageState.ATTACHED)
    plan = dry_run(repository, storage, message_ids=(1,), storage_state=state)
    manifest = MemoryManifest()
    fetcher = DeleteFetcher(include_trash=False)

    with pytest.raises(PermanentError, match="trash folder"):
        execute(
            fetcher,
            repository,
            storage,
            manifest,
            plan=plan,
            mode="trash",
            storage_state=state,
        )

    assert fetcher.calls == []
    assert manifest.events == []


def test_execute_writes_the_manifest_before_updating_the_database() -> None:
    trace: list[str] = []

    class TracingManifest(MemoryManifest):
        def append(self, event: Mapping[str, JSONValue]) -> None:
            trace.append(f"manifest:{event['event']}")
            super().append(event)

        def flush_and_sync(self) -> None:
            trace.append("manifest:fsync")
            super().flush_and_sync()

    class TracingRepository(InMemoryMessageRepository):
        def record_audit(self, entry: Any) -> None:
            trace.append("repo:audit")
            super().record_audit(entry)

        def update_remote_state(
            self,
            message_id: Any,
            state: str,
            moved_to_folder_id: Any = None,
            folder_id: Any | None = None,
        ) -> None:
            trace.append("repo:remote_state")
            super().update_remote_state(message_id, state, moved_to_folder_id, folder_id)

    repository = TracingRepository()
    raw = b"message"
    path = _record(repository, message_id=1, raw=raw)
    storage = MemoryStorage({path: raw})
    state = StorageStateMachine(StorageState.ATTACHED)
    plan = dry_run(repository, storage, message_ids=(1,), storage_state=state)
    manifest = TracingManifest()
    fetcher = DeleteFetcher()
    fetcher.add_message("INBOX", 1, raw)

    execute(fetcher, repository, storage, manifest, plan=plan, storage_state=state)

    assert trace[:4] == [
        "manifest:remote_delete_intent",
        "manifest:fsync",
        "manifest:remote_delete_completed",
        "manifest:fsync",
    ]
    # audit_log and remote_state land in the same DB transaction, committed only
    # after the manifest has been durably written.
    assert set(trace[4:]) == {"repo:audit", "repo:remote_state"}


def test_reconcile_marks_uncertain_delete_complete_when_uid_is_gone() -> None:
    repository = InMemoryMessageRepository()
    raw = b"message"
    path = _record(repository, message_id=1, raw=raw)
    storage = MemoryStorage({path: raw})
    state = StorageStateMachine(StorageState.ATTACHED)
    plan = dry_run(repository, storage, message_ids=(1,), storage_state=state)
    manifest = MemoryManifest()
    manifest.append(
        {
            "event": "remote_delete_intent",
            "account_id": plan.candidates[0].account_id,
            "folder_raw_name": "INBOX",
            "uid": 1,
            "uidvalidity": 42,
            "mode": "trash",
            "timestamp": "2026-08-27T00:00:00+00:00",
        }
    )
    fetcher = DeleteFetcher()
    fetcher.add_message("INBOX", 1, raw)
    fetcher.delete_remote_message("INBOX", 1)

    reconcile_uncertain_deletes(fetcher, repository, manifest, storage_state=state)

    assert repository.messages[1]["remote_state"] == "deleted"
    assert manifest.events[-1]["event"] == "remote_delete_completed"


def test_reconcile_leaves_uncertain_delete_unresolved_when_uid_still_exists() -> None:
    """If the server still has the UID, the delete never happened; it must stay 'present'."""
    repository = InMemoryMessageRepository()
    raw = b"message"
    path = _record(repository, message_id=1, raw=raw)
    storage = MemoryStorage({path: raw})
    state = StorageStateMachine(StorageState.ATTACHED)
    plan = dry_run(repository, storage, message_ids=(1,), storage_state=state)
    manifest = MemoryManifest()
    manifest.append(
        {
            "event": "remote_delete_intent",
            "account_id": plan.candidates[0].account_id,
            "folder_raw_name": "INBOX",
            "uid": 1,
            "uidvalidity": 42,
            "mode": "trash",
            "timestamp": "2026-08-27T00:00:00+00:00",
        }
    )
    fetcher = DeleteFetcher()
    fetcher.add_message("INBOX", 1, raw)  # UID 1 is still on the server; delete was never applied

    reconcile_uncertain_deletes(fetcher, repository, manifest, storage_state=state)

    assert repository.messages[1]["remote_state"] == "present"
    assert [event["event"] for event in manifest.events] == ["remote_delete_intent"]


def _folder_id(repository: InMemoryMessageRepository, raw_name: str) -> Any:
    return next(
        folder["id"] for folder in repository.folders.values() if folder["raw_name"] == raw_name
    )


class CopyUidFetcher(DeleteFetcher):
    """A trash-mode fetcher that confirms the destination UID via COPYUID."""

    def __init__(self, *, dest_uid: int = 99, dest_uidvalidity: int = 7) -> None:
        super().__init__()
        self._dest_uid = dest_uid
        self._dest_uidvalidity = dest_uidvalidity

    def move_remote_message_to_trash(
        self, raw_name: str, uid: int, *, expected_uidvalidity: int | None = None
    ) -> RemoteMoveResult:
        self.calls.append((raw_name, uid, "trash"))
        self._check_expected_uidvalidity(raw_name, expected_uidvalidity)
        return RemoteMoveResult(uidvalidity=self._dest_uidvalidity, uid=self._dest_uid)


def test_execute_trash_mode_links_a_confirmed_move_without_a_duplicate_row() -> None:
    repository = InMemoryMessageRepository()
    raw = b"message"
    path = _record(repository, message_id=1, raw=raw)
    repository.upsert_folder({"account_id": "account", "raw_name": "Trash", "uidvalidity": 7})
    storage = MemoryStorage({path: raw})
    state = StorageStateMachine(StorageState.ATTACHED)
    plan = dry_run(repository, storage, message_ids=(1,), storage_state=state)
    manifest = MemoryManifest()
    fetcher = CopyUidFetcher()

    result = execute(
        fetcher,
        repository,
        storage,
        manifest,
        plan=plan,
        mode="trash",
        storage_state=state,
    )

    assert result.completed_ids == (1,)
    assert [event["event"] for event in manifest.events] == [
        "remote_delete_intent",
        "remote_delete_completed",
        "message_identity_linked",
        "message_membership_snapshot",
    ]
    linked = manifest.events[2]
    alias_key = imap_source_item_key("Trash", 7, 99)
    assert linked["canonical_source_item_key"] == "42:1"
    assert linked["alias_source_item_key"] == alias_key
    assert linked["evidence_kind"] == "copyuid"
    memberships = {
        membership["folder_id"]: membership for membership in repository.message_memberships[1]
    }
    trash_id = _folder_id(repository, "Trash")
    inbox_id = _folder_id(repository, "INBOX")
    assert memberships[trash_id]["remote_state"] == "present"
    assert memberships[inbox_id]["remote_state"] == "moved"
    assert memberships[inbox_id]["moved_to_folder_id"] == trash_id
    assert repository.message_identity_aliases[("account", alias_key)]["message_id"] == 1


def test_execute_trash_mode_merges_into_an_existing_duplicate_row() -> None:
    repository = InMemoryMessageRepository()
    raw = b"message"
    path = _record(repository, message_id=1, raw=raw)
    repository.upsert_folder({"account_id": "account", "raw_name": "Trash", "uidvalidity": 7})
    trash_id = _folder_id(repository, "Trash")
    duplicate_key = imap_source_item_key("Trash", 7, 99)
    repository.add_message(
        {
            "id": 2,
            "account_id": "account",
            "folder_id": trash_id,
            "uid": 99,
            "uidvalidity": 7,
            "source_item_key": duplicate_key,
            "remote_state": "present",
            "local_state": "active",
            "relative_path": path,
            "file_hash": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
            "subject": "Subject 1",
        },
        {"subject": "Subject 1", "body_text": "body"},
    )
    storage = MemoryStorage({path: raw})
    state = StorageStateMachine(StorageState.ATTACHED)
    plan = dry_run(repository, storage, message_ids=(1,), storage_state=state)
    manifest = MemoryManifest()
    fetcher = CopyUidFetcher()

    result = execute(
        fetcher,
        repository,
        storage,
        manifest,
        plan=plan,
        mode="trash",
        storage_state=state,
    )

    assert result.completed_ids == (1,)
    assert 2 not in repository.messages
    memberships = {
        membership["folder_id"]: membership for membership in repository.message_memberships[1]
    }
    assert memberships[trash_id]["remote_state"] == "present"
    inbox_id = _folder_id(repository, "INBOX")
    assert memberships[inbox_id]["remote_state"] == "moved"
    assert memberships[inbox_id]["moved_to_folder_id"] == trash_id


def test_execute_trash_mode_falls_back_when_copyuid_is_unavailable() -> None:
    repository = InMemoryMessageRepository()
    raw = b"message"
    path = _record(repository, message_id=1, raw=raw)
    storage = MemoryStorage({path: raw})
    state = StorageStateMachine(StorageState.ATTACHED)
    plan = dry_run(repository, storage, message_ids=(1,), storage_state=state)
    manifest = MemoryManifest()
    fetcher = DeleteFetcher()
    fetcher.add_message("INBOX", 1, raw)

    result = execute(
        fetcher,
        repository,
        storage,
        manifest,
        plan=plan,
        mode="trash",
        storage_state=state,
    )

    assert result.completed_ids == (1,)
    assert [event["event"] for event in manifest.events] == [
        "remote_delete_intent",
        "remote_delete_completed",
    ]
    assert repository.messages[1]["remote_state"] == "deleted"
