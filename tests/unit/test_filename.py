from pathlib import Path

import pytest

from mail_dock.infrastructure.storage.filename import (
    MAX_FILENAME_BYTES,
    resolve_within,
    sanitize_attachment_name,
)


def test_sanitize_attachment_name_removes_path_components_and_forbidden_characters() -> None:
    result = sanitize_attachment_name(r"../nested\invoice:2026?.txt")

    assert result.name == "invoice_2026_.txt"
    assert "path_component" in result.warnings
    assert "forbidden_character" in result.warnings


def test_sanitize_attachment_name_replaces_control_characters_and_trailing_spaces() -> None:
    result = sanitize_attachment_name("report\n.txt. ")

    assert result.name == "report_.txt"
    assert "control_character" in result.warnings
    assert "trailing_character" in result.warnings


@pytest.mark.parametrize("name", ["CON.txt", "prn", "AUX.log", "COM1.csv", "lpt9.data"])
def test_sanitize_attachment_name_avoids_windows_reserved_names(name: str) -> None:
    result = sanitize_attachment_name(name)

    assert result.name.split(".", 1)[0].casefold().endswith("_")
    assert "reserved_name" in result.warnings


def test_sanitize_attachment_name_uses_fallback_for_empty_name() -> None:
    result = sanitize_attachment_name("../..")

    assert result.name == "attachment"
    assert "empty_name" in result.warnings


def test_sanitize_attachment_name_normalizes_nfc() -> None:
    result = sanitize_attachment_name("e\u0301.txt")

    assert result.name == "é.txt"


def test_sanitize_attachment_name_limits_utf8_bytes_and_preserves_extension() -> None:
    result = sanitize_attachment_name("あ" * 100 + ".pdf")

    assert result.name.endswith(".pdf")
    assert len(result.name.encode("utf-8")) <= MAX_FILENAME_BYTES
    assert "truncated" in result.warnings


@pytest.mark.parametrize("name", ["payload.EXE", "script.js", "launch.PS1"])
def test_sanitize_attachment_name_marks_executable_extensions(name: str) -> None:
    result = sanitize_attachment_name(name)

    assert result.is_executable
    assert result.executable_warning
    assert "executable_extension" in result.warnings


def test_resolve_within_returns_resolved_child(tmp_path: Path) -> None:
    base = tmp_path / "attachments"
    base.mkdir()

    assert resolve_within(base, "invoice.pdf") == base.resolve() / "invoice.pdf"


@pytest.mark.parametrize("name", ["../outside.txt", "/tmp/outside.txt", "."])
def test_resolve_within_rejects_paths_outside_base(tmp_path: Path, name: str) -> None:
    base = tmp_path / "attachments"
    base.mkdir()

    with pytest.raises(ValueError, match="escapes"):
        resolve_within(base, name)


def test_resolve_within_rejects_symlink_escape(tmp_path: Path) -> None:
    base = tmp_path / "attachments"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (base / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes"):
        resolve_within(base, "link/file.txt")