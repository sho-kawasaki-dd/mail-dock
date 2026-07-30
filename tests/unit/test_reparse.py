from __future__ import annotations

import hashlib
from datetime import datetime

from mail_dock.domain.messages import StoredEml
from mail_dock.domain.ports import BaseEmlStorage
from mail_dock.usecases.reparse import ReparseResult, reparse_messages
from tests.support.in_memory_repository import InMemoryMessageRepository


class MemoryEmlStorage(BaseEmlStorage):
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files

    def save(
        self, account_id: str, internal_date: datetime | None, raw: bytes
    ) -> StoredEml:
        del account_id, internal_date, raw
        raise NotImplementedError

    def reuse(self, relative_path: str, expected_hash: str) -> StoredEml | None:
        del relative_path, expected_hash
        raise NotImplementedError

    def read(self, relative_path: str) -> bytes:
        try:
            return self.files[relative_path]
        except KeyError as error:
            raise FileNotFoundError(relative_path) from error


def _raw(subject: str = "Subject") -> bytes:
    return (
        f"From: sender@example.com\nSubject: {subject}\n"
        "Message-ID: <message@example.com>\nContent-Type: text/plain; charset=utf-8\n"
        "\nHello   WORLD\n"
    ).encode()


def _add_message(
    repository: InMemoryMessageRepository,
    *,
    uid: int,
    relative_path: str | None,
    file_hash: str | None,
) -> None:
    repository.add_message(
        {
            "account_id": "account",
            "folder_id": 1,
            "uid": uid,
            "uidvalidity": 10,
            "relative_path": relative_path,
            "file_hash": file_hash,
            "size_bytes": 1,
        }
    )
    repository.record_failure("account", 1, 10, uid, "parse", "old parser")


def test_reparse_updates_contents_and_clears_parse_failure() -> None:
    raw = _raw()
    relative_path = "eml/account/message.eml"
    repository = InMemoryMessageRepository()
    _add_message(
        repository,
        uid=1,
        relative_path=relative_path,
        file_hash=hashlib.sha256(raw).hexdigest(),
    )

    result = reparse_messages(repository, MemoryEmlStorage({relative_path: raw}))

    assert result.reparsed_count == 1
    assert result.skipped_count == 0
    assert repository.contents[1]["body_text"] == "Hello   WORLD\n"
    assert repository.failures == {}
    assert repository.commit_batch_count == 1


def test_reparse_reports_missing_and_hash_mismatch_without_repairing() -> None:
    raw = _raw()
    repository = InMemoryMessageRepository()
    _add_message(repository, uid=1, relative_path="missing.eml", file_hash="a" * 64)
    _add_message(repository, uid=2, relative_path="wrong.eml", file_hash="b" * 64)

    result = reparse_messages(repository, MemoryEmlStorage({"wrong.eml": raw}))

    assert result.reparsed_count == 0
    assert result.skipped_count == 2
    assert result.missing_count == 1
    assert result.hash_mismatch_count == 1
    assert set(repository.failures) == {
        ("account", 1, 10, 1),
        ("account", 1, 10, 2),
    }


def test_reparse_excludes_oversize_rows_without_storage_paths() -> None:
    repository = InMemoryMessageRepository()
    repository.add_message(
        {
            "account_id": "account",
            "folder_id": 1,
            "uid": 3,
            "uidvalidity": 10,
            "relative_path": None,
            "file_hash": None,
        }
    )
    repository.record_failure("account", 1, 10, 3, "oversize", "too large")

    result = reparse_messages(repository, MemoryEmlStorage({}))

    assert result == ReparseResult(0, 0, 0, 0, 0, False)
    assert repository.failures[("account", 1, 10, 3)]["error_class"] == "oversize"