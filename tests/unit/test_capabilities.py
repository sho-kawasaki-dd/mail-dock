from datetime import UTC, datetime
from pathlib import Path

import pytest

from mail_dock.domain.errors import StorageDetachedError
from mail_dock.infrastructure.storage import capabilities
from mail_dock.infrastructure.storage.capabilities import (
    CapabilityLevel,
    StorageCapabilities,
    capability_level,
    journal_mode_for,
    probe_capabilities,
    storage_fingerprint,
)


def _capabilities(**overrides: bool) -> StorageCapabilities:
    values = {
        "exclusive_lock": True,
        "replace_overwrite": True,
        "wal_supported": True,
        "fsync_supported": True,
        "case_sensitive": True,
        "long_path_ok": True,
    }
    values.update(overrides)
    return StorageCapabilities(
        **values,
        checked_at=datetime.now(UTC).isoformat(),
    )


def test_capabilities_round_trip_and_invalid_data() -> None:
    measured = _capabilities()

    assert StorageCapabilities.from_dict(measured.as_dict()) == measured
    assert StorageCapabilities.from_dict({}) is None
    assert StorageCapabilities.from_dict({**measured.as_dict(), "checked_at": "invalid"}) is None


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, CapabilityLevel.OK),
        ({"wal_supported": False}, CapabilityLevel.DEGRADED),
        ({"fsync_supported": False}, CapabilityLevel.DEGRADED),
        ({"exclusive_lock": False}, CapabilityLevel.UNSUPPORTED),
        ({"replace_overwrite": False}, CapabilityLevel.UNSUPPORTED),
    ],
)
def test_capability_level(overrides: dict[str, bool], expected: CapabilityLevel) -> None:
    assert capability_level(_capabilities(**overrides)) is expected


def test_journal_mode_is_conservative() -> None:
    assert journal_mode_for(_capabilities(), network_drive=False) == "WAL"
    assert journal_mode_for(_capabilities(wal_supported=False), network_drive=False) == "DELETE"
    assert journal_mode_for(_capabilities(), network_drive=True) == "DELETE"


def test_probe_uses_only_tmp_and_cleans_up(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / ".lock").write_text("untouched", encoding="ascii")
    (root / "metadata.db").write_text("untouched", encoding="ascii")
    (root / "eml").mkdir()
    (root / "manifests").mkdir()
    (root / "tmp" / "pstimp").mkdir(parents=True)

    measured = probe_capabilities(root)

    assert measured.exclusive_lock
    assert measured.replace_overwrite
    assert measured.wal_supported
    assert measured.fsync_supported
    assert isinstance(measured.case_sensitive, bool)
    assert isinstance(measured.long_path_ok, bool)
    assert measured.checked_at.endswith("+00:00")
    assert capability_level(measured) is CapabilityLevel.OK
    assert (root / ".lock").read_text(encoding="ascii") == "untouched"
    assert (root / "metadata.db").read_text(encoding="ascii") == "untouched"
    assert list((root / "tmp").glob(".captest-*")) == []


def test_storage_fingerprint_includes_normalized_path(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    fingerprint = storage_fingerprint(root)

    assert str(root.resolve()) in fingerprint
    assert fingerprint.startswith("posix:")


def test_detached_error_is_not_swallowed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    detached = StorageDetachedError("root", "unsupported")

    def raise_detached(path: Path) -> bool:
        del path
        raise detached

    monkeypatch.setattr(capabilities, "_probe_exclusive_lock", raise_detached)

    with pytest.raises(StorageDetachedError) as raised:
        probe_capabilities(tmp_path)

    assert raised.value is detached