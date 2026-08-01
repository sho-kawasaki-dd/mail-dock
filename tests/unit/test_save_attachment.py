import errno
import hashlib
from datetime import datetime
from pathlib import Path

import pytest

from mail_dock.domain.errors import StorageError
from mail_dock.domain.messages import MessagePart, RenderedMessage, StoredEml
from mail_dock.domain.ports import BaseEmlStorage, BaseMessageRenderer
from mail_dock.usecases.save_attachment import commit_attachment_save, prepare_attachment_save


class FakeStorage(BaseEmlStorage):
    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.read_count = 0

    def save(self, account_id: str, internal_date: datetime | None, raw: bytes) -> StoredEml:
        raise NotImplementedError

    def reuse(self, relative_path: str, expected_hash: str) -> StoredEml | None:
        raise NotImplementedError

    def read(self, relative_path: str) -> bytes:
        raise NotImplementedError

    def read_verified(self, relative_path: str, expected_hash: str) -> bytes:
        self.read_count += 1
        if hashlib.sha256(self.raw).hexdigest() != expected_hash:
            raise StorageError("hash mismatch")
        return self.raw


class FakeRenderer(BaseMessageRenderer):
    def __init__(self, part: MessagePart) -> None:
        self.part = part

    def render(self, raw: bytes) -> RenderedMessage:
        return RenderedMessage(None, "", (self.part,))


def _ports(tmp_path: Path, filename: str = "invoice.pdf") -> tuple[FakeStorage, FakeRenderer]:
    storage = FakeStorage(b"eml")
    renderer = FakeRenderer(MessagePart(None, "application/pdf", filename, b"payload", False))
    return storage, renderer


def test_prepare_does_not_create_a_file_and_commit_is_atomic(tmp_path: Path) -> None:
    storage, renderer = _ports(tmp_path)
    expected_hash = hashlib.sha256(storage.raw).hexdigest()

    plan = prepare_attachment_save(
        storage,
        renderer,
        relative_path="eml/mail.eml",
        expected_hash=expected_hash,
        part_index=0,
        dest_dir=tmp_path,
    )

    assert not list(tmp_path.iterdir())
    saved = commit_attachment_save(storage, renderer, plan=plan)
    assert saved.path.read_bytes() == b"payload"
    assert saved.path.name == "invoice.pdf"
    assert not list(tmp_path.glob(".mail-dock-attachment-*.tmp"))


def test_prepare_sanitizes_and_warns_about_executable_name(tmp_path: Path) -> None:
    storage, renderer = _ports(tmp_path, "../script.PS1")
    expected_hash = hashlib.sha256(storage.raw).hexdigest()

    plan = prepare_attachment_save(
        storage,
        renderer,
        relative_path="eml/mail.eml",
        expected_hash=expected_hash,
        part_index=0,
        dest_dir=tmp_path,
    )

    assert plan.filename == "script.PS1"
    assert plan.is_executable
    assert "path_component" in plan.warnings


def test_prepare_reports_path_reserved_and_trailing_name_warnings(tmp_path: Path) -> None:
    storage, renderer = _ports(tmp_path, "../CON.txt. ")
    expected_hash = hashlib.sha256(storage.raw).hexdigest()

    plan = prepare_attachment_save(
        storage,
        renderer,
        relative_path="eml/mail.eml",
        expected_hash=expected_hash,
        part_index=0,
        dest_dir=tmp_path,
    )

    assert plan.filename == "CON_.txt"
    assert {"path_component", "reserved_name", "trailing_character"}.issubset(
        plan.warnings
    )


def test_commit_rejects_a_destination_symlink_created_after_prepare(tmp_path: Path) -> None:
    destination = tmp_path / "attachments"
    destination.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    storage, renderer = _ports(tmp_path, "link")
    expected_hash = hashlib.sha256(storage.raw).hexdigest()
    plan = prepare_attachment_save(
        storage,
        renderer,
        relative_path="eml/mail.eml",
        expected_hash=expected_hash,
        part_index=0,
        dest_dir=destination,
    )
    try:
        (destination / plan.filename).symlink_to(outside, target_is_directory=True)
    except OSError as error:
        if (
            error.errno in {errno.EACCES, errno.EPERM, errno.ENOSYS}
            or getattr(error, "winerror", None) == 1314
        ):
            pytest.skip("symlink creation is unavailable in this environment")
        raise

    with pytest.raises(StorageError, match="safe"):
        commit_attachment_save(storage, renderer, plan=plan)

    assert not list(outside.iterdir())


def test_explicit_filename_is_used_and_revalidated_at_commit(tmp_path: Path) -> None:
    storage, renderer = _ports(tmp_path, "original.txt")
    expected_hash = hashlib.sha256(storage.raw).hexdigest()
    plan = prepare_attachment_save(
        storage,
        renderer,
        relative_path="eml/mail.eml",
        expected_hash=expected_hash,
        part_index=0,
        dest_dir=tmp_path,
        filename="renamed.txt",
    )

    saved = commit_attachment_save(storage, renderer, plan=plan)

    assert saved.path.name == "renamed.txt"


def test_commit_rechecks_the_eml_hash_after_prepare(tmp_path: Path) -> None:
    storage, renderer = _ports(tmp_path)
    expected_hash = hashlib.sha256(storage.raw).hexdigest()
    plan = prepare_attachment_save(
        storage,
        renderer,
        relative_path="eml/mail.eml",
        expected_hash=expected_hash,
        part_index=0,
        dest_dir=tmp_path,
    )
    storage.raw = b"changed EML"

    with pytest.raises(StorageError, match="hash"):
        commit_attachment_save(storage, renderer, plan=plan)


def test_commit_cleans_temporary_file_when_writing_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, renderer = _ports(tmp_path)
    expected_hash = hashlib.sha256(storage.raw).hexdigest()
    plan = prepare_attachment_save(
        storage,
        renderer,
        relative_path="eml/mail.eml",
        expected_hash=expected_hash,
        part_index=0,
        dest_dir=tmp_path,
    )
    monkeypatch.setattr(
        "mail_dock.usecases.save_attachment.os.fsync",
        lambda descriptor: (_ for _ in ()).throw(OSError("fsync failed")),
    )

    with pytest.raises(StorageError, match="save attachment"):
        commit_attachment_save(storage, renderer, plan=plan)

    assert not list(tmp_path.glob(".mail-dock-attachment-*.tmp"))


def test_commit_uses_a_numbered_name_when_plan_name_now_exists(tmp_path: Path) -> None:
    storage, renderer = _ports(tmp_path)
    expected_hash = hashlib.sha256(storage.raw).hexdigest()
    plan = prepare_attachment_save(
        storage,
        renderer,
        relative_path="eml/mail.eml",
        expected_hash=expected_hash,
        part_index=0,
        dest_dir=tmp_path,
    )
    (tmp_path / "invoice.pdf").write_bytes(b"existing")

    saved = commit_attachment_save(storage, renderer, plan=plan)

    assert saved.path.name == "invoice (1).pdf"
    assert (tmp_path / "invoice.pdf").read_bytes() == b"existing"


def test_commit_rejects_inline_parts_and_invalid_part_indexes(tmp_path: Path) -> None:
    storage = FakeStorage(b"eml")
    expected_hash = hashlib.sha256(storage.raw).hexdigest()
    inline_renderer = FakeRenderer(MessagePart("cid", "image/png", "image.png", b"x", True))

    with pytest.raises(StorageError, match="Inline"):
        prepare_attachment_save(
            storage,
            inline_renderer,
            relative_path="eml/mail.eml",
            expected_hash=expected_hash,
            part_index=0,
            dest_dir=tmp_path,
        )
    with pytest.raises(StorageError, match="out of range"):
        prepare_attachment_save(
            storage,
            FakeRenderer(MessagePart(None, "application/octet-stream", None, b"x", False)),
            relative_path="eml/mail.eml",
            expected_hash=expected_hash,
            part_index=1,
            dest_dir=tmp_path,
        )
