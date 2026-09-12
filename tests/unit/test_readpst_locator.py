from pathlib import Path

import pytest

from mail_dock.domain.errors import ConverterNotFound
from mail_dock.infrastructure.importers.readpst_locator import (
    _WINDOWS_READPST_DLLS,
    ReadPstLocator,
)


def _write_windows_bundle(root: Path) -> None:
    (root / "readpst.exe").write_bytes(b"converter")
    for name in _WINDOWS_READPST_DLLS:
        (root / name).write_bytes(b"dll")


def test_locator_returns_absolute_readpst_path_and_checks_dlls(tmp_path: Path) -> None:
    _write_windows_bundle(tmp_path)

    locator = ReadPstLocator(tmp_path, is_windows=True)

    assert locator.locate() == tmp_path.resolve() / "readpst.exe"
    assert locator.vendor_dir == tmp_path.resolve()


def test_locator_rejects_missing_windows_dependency(tmp_path: Path) -> None:
    _write_windows_bundle(tmp_path)
    (tmp_path / _WINDOWS_READPST_DLLS[0]).unlink()

    with pytest.raises(ConverterNotFound, match=_WINDOWS_READPST_DLLS[0]):
        ReadPstLocator(tmp_path, is_windows=True).locate()


def test_locator_reads_version_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_windows_bundle(tmp_path)
    calls: list[tuple[list[str], dict[str, object]]] = []

    class Completed:
        returncode = 0
        stdout = "readpst version 0.6.76\n"
        stderr = ""

    def fake_run(command: list[str], **kwargs: object) -> Completed:
        calls.append((command, kwargs))
        return Completed()

    monkeypatch.setattr(
        "mail_dock.infrastructure.importers.readpst_locator.subprocess.run", fake_run
    )

    assert ReadPstLocator(tmp_path, is_windows=True).version() == "readpst version 0.6.76"
    assert calls[0][0] == [str(tmp_path.resolve() / "readpst.exe"), "-V"]
    assert calls[0][1]["cwd"] == tmp_path.resolve()


def test_locator_rejects_failed_version_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_windows_bundle(tmp_path)

    class Completed:
        returncode = 1
        stdout = ""
        stderr = "failed"

    monkeypatch.setattr(
        "mail_dock.infrastructure.importers.readpst_locator.subprocess.run",
        lambda *args, **kwargs: Completed(),
    )

    with pytest.raises(ConverterNotFound, match="version check failed"):
        ReadPstLocator(tmp_path, is_windows=True).version()
