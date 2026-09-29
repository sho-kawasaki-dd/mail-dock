import sys
from pathlib import Path

import pytest

from mail_dock.infrastructure import app_paths


def test_bundle_root_uses_meipass_when_frozen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert app_paths.is_frozen()
    assert app_paths.bundle_root() == tmp_path


def test_bundle_root_uses_repository_root_when_not_frozen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    assert not app_paths.is_frozen()
    assert app_paths.bundle_root() == Path(app_paths.__file__).resolve().parents[3]
