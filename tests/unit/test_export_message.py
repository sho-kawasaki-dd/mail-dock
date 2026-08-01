import hashlib
from datetime import datetime
from pathlib import Path

import pytest

from mail_dock.domain.errors import StorageError
from mail_dock.domain.messages import StoredEml
from mail_dock.domain.ports import BaseEmlStorage
from mail_dock.usecases.export_message import export_eml


class FakeStorage(BaseEmlStorage):
    def __init__(self, raw: bytes) -> None:
        self.raw = raw

    def save(self, account_id: str, internal_date: datetime | None, raw: bytes) -> StoredEml:
        raise NotImplementedError

    def reuse(self, relative_path: str, expected_hash: str) -> StoredEml | None:
        raise NotImplementedError

    def read(self, relative_path: str) -> bytes:
        raise NotImplementedError

    def read_verified(self, relative_path: str, expected_hash: str) -> bytes:
        if hashlib.sha256(self.raw).hexdigest() != expected_hash:
            raise StorageError("EML file hash does not match expected hash")
        return self.raw


def test_export_eml_writes_verified_bytes_to_the_requested_path(tmp_path: Path) -> None:
    storage = FakeStorage(b"Subject: exported\n\nbody\n")
    destination = tmp_path / "mail.eml"
    expected_hash = hashlib.sha256(storage.raw).hexdigest()

    result = export_eml(
        storage,
        relative_path="eml/source.eml",
        expected_hash=expected_hash,
        dest_path=destination,
    )

    assert result == destination
    assert destination.read_bytes() == storage.raw
    assert not list(tmp_path.glob(".mail-dock-export-*.tmp"))


def test_export_eml_rejects_a_hash_mismatch_without_creating_a_file(tmp_path: Path) -> None:
    storage = FakeStorage(b"actual")
    destination = tmp_path / "mail.eml"

    with pytest.raises(StorageError, match="hash"):
        export_eml(
            storage,
            relative_path="eml/source.eml",
            expected_hash="0" * 64,
            dest_path=destination,
        )

    assert not destination.exists()


def test_export_eml_cleans_the_same_directory_temporary_file_on_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FakeStorage(b"export")
    destination = tmp_path / "mail.eml"
    expected_hash = hashlib.sha256(storage.raw).hexdigest()
    monkeypatch.setattr(
        "mail_dock.usecases.export_message.os.replace",
        lambda source, target: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(StorageError, match="export"):
        export_eml(
            storage,
            relative_path="eml/source.eml",
            expected_hash=expected_hash,
            dest_path=destination,
        )

    assert not destination.exists()
    assert not list(tmp_path.glob(".mail-dock-export-*.tmp"))


def test_export_eml_rejects_a_parent_that_changes_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_parent = tmp_path / "first"
    second_parent = tmp_path / "second"
    first_parent.mkdir()
    second_parent.mkdir()
    alias = tmp_path / "destination"
    alias.symlink_to(first_parent, target_is_directory=True)
    destination = alias / "mail.eml"
    storage = FakeStorage(b"export")
    expected_hash = hashlib.sha256(storage.raw).hexdigest()
    original_resolve = Path.resolve
    resolve_count = 0

    def changing_resolve(path: Path, *, strict: bool = False) -> Path:
        nonlocal resolve_count
        resolved = original_resolve(path, strict=strict)
        if path == alias:
            resolve_count += 1
            if resolve_count == 1:
                alias.unlink()
                alias.symlink_to(second_parent, target_is_directory=True)
        return resolved

    monkeypatch.setattr(Path, "resolve", changing_resolve)

    with pytest.raises(StorageError, match="changed"):
        export_eml(
            storage,
            relative_path="eml/source.eml",
            expected_hash=expected_hash,
            dest_path=destination,
        )

    assert not (first_parent / "mail.eml").exists()
    assert not (second_parent / "mail.eml").exists()
