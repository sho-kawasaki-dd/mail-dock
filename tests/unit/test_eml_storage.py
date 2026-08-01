import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mail_dock.domain.errors import StorageDetachedError, StorageError
from mail_dock.infrastructure.storage.eml_storage import (
    EmlStorage,
    cleanup_tmp,
    save_eml,
    validate_account_id,
)


def test_save_eml_uses_hash_and_internal_date(tmp_storage_root: Path) -> None:
    raw = b"Subject: hello\n\nbody\n"

    stored = save_eml(
        tmp_storage_root,
        "account@example.com",
        datetime(2026, 7, 30, 12, tzinfo=UTC),
        raw,
    )

    expected_hash = hashlib.sha256(raw).hexdigest()
    assert stored.relative_path == f"eml/account@example.com/2026/07/{expected_hash[:32]}.eml"
    assert stored.file_hash == expected_hash
    assert stored.size_bytes == len(raw)
    assert not stored.deduplicated
    assert (tmp_storage_root / stored.relative_path).read_bytes() == raw
    assert list((tmp_storage_root / "tmp").iterdir()) == []


def test_save_eml_uses_unknown_directory_without_internal_date(tmp_storage_root: Path) -> None:
    stored = save_eml(tmp_storage_root, "account", None, b"body")

    assert stored.relative_path.startswith("eml/account/unknown/")


def test_deduplication_validates_complete_hash_without_writing_tmp(
    tmp_storage_root: Path,
) -> None:
    storage = EmlStorage(tmp_storage_root)
    raw = b"same content"
    first = storage.save("account", None, raw)

    second = storage.save("account", datetime(2030, 1, 1, tzinfo=UTC), raw)

    assert second.relative_path == first.relative_path
    assert second.file_hash == first.file_hash
    assert second.size_bytes == first.size_bytes
    assert second.deduplicated
    assert not list((tmp_storage_root / "tmp").iterdir())


def test_same_prefix_with_wrong_full_hash_is_not_reused(tmp_storage_root: Path) -> None:
    raw = b"new"
    file_hash = hashlib.sha256(raw).hexdigest()
    destination = tmp_storage_root / "eml" / "account" / "unknown" / f"{file_hash[:32]}.eml"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"different content")

    stored = save_eml(tmp_storage_root, "account", None, raw)

    assert not stored.deduplicated
    assert destination.read_bytes() == raw


def test_save_cleans_tmp_when_replace_fails(
    tmp_storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "mail_dock.infrastructure.storage.eml_storage.os.replace",
        lambda source, destination: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(OSError, match="replace failed"):
        save_eml(tmp_storage_root, "account", None, b"body")

    assert list((tmp_storage_root / "tmp").iterdir()) == []


def test_cleanup_tmp_leaves_pst_import_staging(tmp_storage_root: Path) -> None:
    (tmp_storage_root / "tmp" / "pstimp").mkdir(parents=True)
    (tmp_storage_root / "tmp" / "pstimp" / "keep").write_bytes(b"pst")
    (tmp_storage_root / "tmp" / "orphan.eml").write_bytes(b"orphan")

    assert cleanup_tmp(tmp_storage_root) == 1
    assert (tmp_storage_root / "tmp" / "pstimp" / "keep").exists()
    assert not (tmp_storage_root / "tmp" / "orphan.eml").exists()


@pytest.mark.parametrize("account_id", ["", ".", "..", "a/b", "a\\b", "CON", "mail."])
def test_validate_account_id_rejects_unsafe_values(account_id: str) -> None:
    with pytest.raises(ValueError):
        validate_account_id(account_id)


def test_read_and_reuse_reject_paths_outside_root(tmp_storage_root: Path) -> None:
    storage = EmlStorage(tmp_storage_root)

    with pytest.raises(ValueError, match="escapes"):
        storage.read("../outside.eml")
    with pytest.raises(ValueError, match="escapes"):
        storage.read_verified("../outside.eml", "0" * 64)
    with pytest.raises(ValueError, match="escapes"):
        storage.reuse("../outside.eml", "0" * 64)


def test_read_verified_returns_bytes_only_for_a_matching_complete_hash(
    tmp_storage_root: Path,
) -> None:
    storage = EmlStorage(tmp_storage_root)
    raw = b"verified EML content"
    stored = storage.save("account", None, raw)

    assert storage.read_verified(stored.relative_path, stored.file_hash) == raw

    with pytest.raises(StorageError, match="hash"):
        storage.read_verified(stored.relative_path, "0" * 64)


def test_detached_storage_error_is_classified(
    tmp_storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "mail_dock.infrastructure.storage.eml_storage.os.replace",
        lambda source, destination: (_ for _ in ()).throw(OSError(5, "device detached")),
    )

    with pytest.raises(StorageDetachedError):
        save_eml(tmp_storage_root, "account", None, b"body")
