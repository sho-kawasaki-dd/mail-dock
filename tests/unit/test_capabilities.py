import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

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


@pytest.mark.parametrize(
    ("probe_name", "field_name", "expected_level"),
    [
        ("_probe_exclusive_lock", "exclusive_lock", CapabilityLevel.UNSUPPORTED),
        ("_probe_replace_overwrite", "replace_overwrite", CapabilityLevel.UNSUPPORTED),
        ("_probe_wal", "wal_supported", CapabilityLevel.DEGRADED),
        ("_probe_fsync", "fsync_supported", CapabilityLevel.DEGRADED),
        ("_probe_case_sensitivity", "case_sensitive", CapabilityLevel.OK),
        ("_probe_long_path", "long_path_ok", CapabilityLevel.OK),
    ],
)
def test_probe_failure_is_recorded_and_aggregated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    probe_name: str,
    field_name: str,
    expected_level: CapabilityLevel,
) -> None:
    root = tmp_path / "root"
    root.mkdir()

    def failed_probe(*args: Path) -> bool:
        del args
        return False

    monkeypatch.setattr(capabilities, probe_name, failed_probe)

    measured = probe_capabilities(root)

    assert getattr(measured, field_name) is False
    assert capability_level(measured) is expected_level


def test_journal_mode_is_conservative() -> None:
    assert journal_mode_for(_capabilities(), network_drive=False) == "WAL"
    assert journal_mode_for(_capabilities(wal_supported=False), network_drive=False) == "DELETE"
    assert journal_mode_for(_capabilities(), network_drive=True) == "DELETE"


def test_exclusive_lock_probe_reaps_competing_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    processes: list[subprocess.Popen[bytes]] = []
    original_popen = subprocess.Popen

    def tracking_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        process = cast("subprocess.Popen[bytes]", original_popen(*args, **kwargs))
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", tracking_popen)

    assert capabilities._probe_exclusive_lock(tmp_dir / ".captest-lock") is True
    assert len(processes) == 1
    assert processes[0].poll() is not None


def test_probe_uses_only_tmp_and_cleans_up(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / ".lock").write_text("untouched", encoding="ascii")
    (root / "metadata.db").write_text("untouched", encoding="ascii")
    (root / "eml" / "keep.eml").parent.mkdir()
    (root / "eml" / "keep.eml").write_text("untouched", encoding="ascii")
    (root / "manifests" / "keep.jsonl").parent.mkdir()
    (root / "manifests" / "keep.jsonl").write_text("untouched", encoding="ascii")
    (root / "tmp" / "pstimp" / "keep.eml").parent.mkdir(parents=True)
    (root / "tmp" / "pstimp" / "keep.eml").write_text("untouched", encoding="ascii")
    before = {
        path.relative_to(root): path.read_bytes() if path.is_file() else None
        for path in root.rglob("*")
    }

    measured = probe_capabilities(root)

    after = {
        path.relative_to(root): path.read_bytes() if path.is_file() else None
        for path in root.rglob("*")
    }

    assert measured.exclusive_lock
    assert measured.replace_overwrite
    assert measured.wal_supported
    assert measured.fsync_supported
    assert isinstance(measured.case_sensitive, bool)
    assert isinstance(measured.long_path_ok, bool)
    assert measured.checked_at.endswith("+00:00")
    assert capability_level(measured) is CapabilityLevel.OK
    assert before == after
    assert list((root / "tmp").glob(".captest-*")) == []


def test_probe_cleans_tmp_after_unexpected_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "root"
    root.mkdir()

    def raise_unexpected(*args: Path) -> bool:
        del args
        raise RuntimeError("probe failure")

    monkeypatch.setattr(capabilities, "_probe_wal", raise_unexpected)

    with pytest.raises(RuntimeError, match="probe failure"):
        probe_capabilities(root)

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
    assert list((tmp_path / "tmp").glob(".captest-*")) == []


def test_capabilities_module_imports_without_qt() -> None:
    script = """
import sys
import mail_dock.infrastructure.storage.capabilities

print(any(name == "PySide6" or name.startswith("PySide6.") for name in sys.modules))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "False"