from __future__ import annotations

import hashlib
import mailbox
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pytest

from mail_dock.domain.errors import OperationCancelledError, StorageError
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.messages import StoredEml
from mail_dock.domain.ports import BaseEmlStorage
from mail_dock.domain.repository import BaseMessageRepository
from mail_dock.usecases.export_mbox import ExportMboxProgress, export_mbox


class FakeStorage(BaseEmlStorage):
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.read_verified_calls: list[tuple[str, str]] = []

    def save(self, account_id: str, internal_date: datetime | None, raw: bytes) -> StoredEml:
        raise NotImplementedError

    def reuse(self, relative_path: str, expected_hash: str) -> StoredEml | None:
        raise NotImplementedError

    def read(self, relative_path: str) -> bytes:
        return self.files[relative_path]

    def read_verified(self, relative_path: str, expected_hash: str) -> bytes:
        self.read_verified_calls.append((relative_path, expected_hash))
        raw = self.files[relative_path]
        if hashlib.sha256(raw).hexdigest() != expected_hash:
            raise StorageError("EML file hash does not match expected hash")
        return raw


class FakeRepository:
    def __init__(self, messages: dict[int, dict[str, Any]]) -> None:
        self.messages = messages

    def get_message(self, message_id: Any) -> dict[str, Any] | None:
        return self.messages.get(int(message_id))


def _record(message_id: int, raw: bytes, *, local_state: str = "active") -> dict[str, Any]:
    return {
        "id": message_id,
        "local_state": local_state,
        "relative_path": f"eml/{message_id}.eml",
        "file_hash": hashlib.sha256(raw).hexdigest(),
    }


def test_export_mbox_is_readable_and_verifies_each_eml(tmp_path: Path) -> None:
    first = b"From: first@example.test\n\nfirst body\n"
    second = b"From: second@example.test\n\nsecond body\n"
    repo = cast(
        BaseMessageRepository,
        FakeRepository({1: _record(1, first), 2: _record(2, second)}),
    )
    storage = FakeStorage({"eml/1.eml": first, "eml/2.eml": second})
    destination = tmp_path / "archive.mbox"

    result = export_mbox(
        repo,
        storage,
        message_ids=[1, 2],
        dest_path=destination,
    )

    assert result == destination
    exported = mailbox.mbox(str(destination))
    try:
        assert [message.get_payload(decode=True) for message in exported] == [
            b"first body\n",
            b"second body\n",
        ]
    finally:
        exported.close()
    assert len(storage.read_verified_calls) == 2
    assert not list(tmp_path.glob(".mail-dock-mbox-*.tmp"))


def test_export_mbox_skips_purged_messages_and_reports_the_count(tmp_path: Path) -> None:
    raw = b"Subject: retained\n\nbody\n"
    repo = cast(
        BaseMessageRepository,
        FakeRepository({1: _record(1, raw), 2: _record(2, b"gone", local_state="purged")}),
    )
    storage = FakeStorage({"eml/1.eml": raw})
    progress: list[ExportMboxProgress] = []

    export_mbox(
        repo,
        storage,
        message_ids=[1, 2],
        dest_path=tmp_path / "archive.mbox",
        on_progress=progress.append,
    )

    assert progress[-1].exported_count == 1
    assert progress[-1].skipped_count == 1
    assert progress[-1].processed_count == 2
    assert storage.read_verified_calls == [("eml/1.eml", hashlib.sha256(raw).hexdigest())]


def test_export_mbox_removes_temporary_file_after_hash_failure(tmp_path: Path) -> None:
    raw = b"actual"
    repo = cast(
        BaseMessageRepository,
        FakeRepository({1: {**_record(1, raw), "file_hash": "0" * 64}}),
    )
    storage = FakeStorage({"eml/1.eml": raw})
    destination = tmp_path / "archive.mbox"

    with pytest.raises(StorageError, match="hash"):
        export_mbox(repo, storage, message_ids=[1], dest_path=destination)

    assert not destination.exists()
    assert not list(tmp_path.glob(".mail-dock-mbox-*.tmp"))


def test_export_mbox_cancellation_does_not_publish_partial_output(tmp_path: Path) -> None:
    raw = b"Subject: message\n\nbody\n"
    repo = cast(BaseMessageRepository, FakeRepository({1: _record(1, raw), 2: _record(2, raw)}))
    storage = FakeStorage({"eml/1.eml": raw, "eml/2.eml": raw})
    token = CancelToken()

    def cancel_after_first(progress: ExportMboxProgress) -> None:
        if progress.processed_count == 1:
            token.cancel()

    destination = tmp_path / "archive.mbox"
    with pytest.raises(OperationCancelledError, match="cancelled"):
        export_mbox(
            repo,
            storage,
            message_ids=[1, 2],
            dest_path=destination,
            cancel=token,
            on_progress=cancel_after_first,
        )

    assert not destination.exists()
    assert not list(tmp_path.glob(".mail-dock-mbox-*.tmp"))
