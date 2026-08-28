from pathlib import Path

import pytest

from mail_dock.domain.errors import InsufficientSpaceError, StorageDetachedError
from mail_dock.infrastructure.storage import storage_root
from mail_dock.infrastructure.storage.storage_root import (
    MINIMUM_FREE_BYTES,
    WARNING_FREE_BYTES,
    RootProbe,
    SpaceStatus,
    ensure_layout,
    initialize_root,
    probe,
    resolve_root,
)


def test_probe_reports_ok_missing_and_foreign(
    tmp_path: Path,
    tmp_storage_root: Path,
) -> None:
    marker = initialize_root(tmp_storage_root)
    foreign_root = tmp_path / "foreign"
    foreign_marker = initialize_root(foreign_root)

    assert probe(tmp_storage_root, marker.root_uuid) is RootProbe.OK
    assert probe(tmp_path / "missing", None) is RootProbe.MISSING
    assert probe(foreign_root, marker.root_uuid) is RootProbe.FOREIGN
    assert foreign_marker.root_uuid != marker.root_uuid


def test_probe_classifies_detached_error_while_checking_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_storage_root: Path,
) -> None:
    initialize_root(tmp_storage_root)

    def raise_detached(_path: Path) -> bool:
        raise OSError(5, "detached")

    monkeypatch.setattr(Path, "is_file", raise_detached)

    with pytest.raises(StorageDetachedError):
        probe(tmp_storage_root, "root-uuid")


def test_resolve_root_follows_matching_candidate(tmp_path: Path, tmp_storage_root: Path) -> None:
    marker = initialize_root(tmp_storage_root)
    resolution = resolve_root(
        [tmp_path / "new-drive-letter", tmp_storage_root, tmp_storage_root],
        marker.root_uuid,
    )

    assert resolution.path == tmp_storage_root.resolve()
    assert resolution.probe is RootProbe.OK


def test_ensure_layout_creates_all_storage_directories(tmp_path: Path) -> None:
    root = tmp_path / "root"

    ensure_layout(root)

    for relative_path in ("eml", "manifests/imap", "manifests/pst", "tmp", "logs"):
        assert (root / relative_path).is_dir()


def test_check_free_space_reports_warning(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        storage_root,
        "free_space",
        lambda path: WARNING_FREE_BYTES - 1,
    )

    assert storage_root.check_free_space(tmp_path) is SpaceStatus.WARNING


def test_check_free_space_rejects_insufficient_space(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        storage_root,
        "free_space",
        lambda path: MINIMUM_FREE_BYTES - 1,
    )

    with pytest.raises(InsufficientSpaceError):
        storage_root.check_free_space(tmp_path)
