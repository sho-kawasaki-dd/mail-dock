from __future__ import annotations

import errno
import hashlib
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from mail_dock.domain.errors import StorageError
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.messages import MessagePart, RenderedMessage, StoredEml
from mail_dock.domain.ports import BaseEmlStorage, BaseMessageRenderer
from mail_dock.domain.search import MessageDetail
from mail_dock.usecases.export_attachments import ExportAttachmentsProgress, export_attachments


class FakeStorage(BaseEmlStorage):
    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.reads: list[tuple[str, str]] = []

    def save(self, account_id: str, internal_date: datetime | None, raw: bytes) -> StoredEml:
        raise NotImplementedError

    def reuse(self, relative_path: str, expected_hash: str) -> StoredEml | None:
        raise NotImplementedError

    def read(self, relative_path: str) -> bytes:
        raise NotImplementedError

    def read_verified(self, relative_path: str, expected_hash: str) -> bytes:
        self.reads.append((relative_path, expected_hash))
        assert hashlib.sha256(self.raw).hexdigest() == expected_hash
        return self.raw


class FakeRenderer(BaseMessageRenderer):
    def __init__(self, parts: tuple[MessagePart, ...]) -> None:
        self.parts = parts

    def render(self, raw: bytes) -> RenderedMessage:
        del raw
        return RenderedMessage(None, "", self.parts)


def _message(local_state: str = "active") -> dict[str, object]:
    raw = b"eml"
    return {
        "id": 7,
        "local_state": local_state,
        "relative_path": "eml/message.eml",
        "file_hash": hashlib.sha256(raw).hexdigest(),
    }


def test_exports_regular_attachments_and_excludes_content_id_parts(tmp_path: Path) -> None:
    storage = FakeStorage(b"eml")
    renderer = FakeRenderer(
        (
            MessagePart(None, "application/pdf", "invoice.pdf", b"pdf", False),
            MessagePart("cid-image", "image/png", "inline.png", b"png", False),
            MessagePart("cid-inline", "image/jpeg", "inline-2.jpg", b"jpg", True),
        )
    )

    result = export_attachments(storage, renderer, messages=(_message(),), dest_dir=tmp_path)

    assert [path.name for path in result.files] == ["invoice.pdf"]
    assert (tmp_path / "invoice.pdf").read_bytes() == b"pdf"
    assert not (tmp_path / "inline.png").exists()
    assert not (tmp_path / "inline-2.jpg").exists()


def test_accepts_message_detail_objects_returned_by_repository(tmp_path: Path) -> None:
    storage = FakeStorage(b"eml")
    renderer = FakeRenderer((MessagePart(None, "text/plain", "note.txt", b"note", False),))
    message = cast(MessageDetail, SimpleNamespace(**_message()))

    result = export_attachments(storage, renderer, messages=(message,), dest_dir=tmp_path)

    assert result.files == (tmp_path / "note.txt",)


def test_uses_numbered_names_and_reports_executable_warning(tmp_path: Path) -> None:
    (tmp_path / "script.ps1").write_bytes(b"existing")
    storage = FakeStorage(b"eml")
    renderer = FakeRenderer(
        (MessagePart(None, "application/octet-stream", "script.ps1", b"new", False),)
    )

    result = export_attachments(storage, renderer, messages=(_message(),), dest_dir=tmp_path)

    assert result.files == (tmp_path / "script (1).ps1",)
    assert (tmp_path / "script.ps1").read_bytes() == b"existing"
    assert "executable_extension" in result.warnings
    assert result.executable_paths == result.files
    assert result.warning_details[0].path == result.files[0]


def test_sanitizes_path_traversal_attempts_in_attachment_filename(tmp_path: Path) -> None:
    storage = FakeStorage(b"eml")
    renderer = FakeRenderer(
        (MessagePart(None, "text/plain", "../../secret.txt", b"payload", False),)
    )

    result = export_attachments(storage, renderer, messages=(_message(),), dest_dir=tmp_path)

    assert result.files == (tmp_path / "secret.txt",)
    assert (tmp_path / "secret.txt").read_bytes() == b"payload"
    assert not (tmp_path.parent / "secret.txt").exists()


def test_rejects_a_filename_that_resolves_outside_the_destination_via_symlink(
    tmp_path: Path,
) -> None:
    dest_dir = tmp_path / "attachments"
    dest_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (dest_dir / "escape").symlink_to(outside, target_is_directory=True)
    except OSError as error:
        if (
            error.errno in {errno.EACCES, errno.EPERM, errno.ENOSYS}
            or getattr(error, "winerror", None) == 1314
        ):
            pytest.skip("symlink creation is unavailable in this environment")
        raise
    storage = FakeStorage(b"eml")
    renderer = FakeRenderer((MessagePart(None, "text/plain", "escape", b"payload", False),))

    with pytest.raises(StorageError, match="no longer safe"):
        export_attachments(storage, renderer, messages=(_message(),), dest_dir=dest_dir)

    assert list(outside.iterdir()) == []


def test_skips_purged_messages_and_reports_progress(tmp_path: Path) -> None:
    progress: list[ExportAttachmentsProgress] = []
    storage = FakeStorage(b"eml")
    renderer = FakeRenderer((MessagePart(None, "text/plain", "note.txt", b"note", False),))

    result = export_attachments(
        storage,
        renderer,
        messages=(_message("purged"), _message()),
        dest_dir=tmp_path,
        on_progress=progress.append,
    )

    assert result.skipped_count == 1
    assert result.exported_count == 1
    assert [item.processed_count for item in progress] == [1, 2]
    assert progress[-1].exported_count == 1


def test_cancellation_leaves_no_temporary_file(tmp_path: Path) -> None:
    cancel = CancelToken()
    storage = FakeStorage(b"eml")
    renderer = FakeRenderer((MessagePart(None, "text/plain", "note.txt", b"note", False),))

    def stop_after_first(progress: object) -> None:
        del progress
        cancel.cancel()

    with pytest.raises(Exception, match="cancel"):
        export_attachments(
            storage,
            renderer,
            messages=(_message(), _message()),
            dest_dir=tmp_path,
            cancel=cancel,
            on_progress=stop_after_first,
        )

    assert not list(tmp_path.glob(".mail-dock-attachment-*.tmp"))
