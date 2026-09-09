import struct
from pathlib import Path

from mail_dock.domain.importer import ArchiveFolder
from mail_dock.infrastructure.importers.lspst_parser import (
    LspstParser,
    detect_pst_format,
    detect_pst_type,
    parse_lspst_output,
)


def test_parser_keeps_folder_lines_and_ignores_partial_lspst_diagnostics() -> None:
    output = (
        'Folder "受信トレイ"\r\n'
        "Email\tFrom: sender@example.com\tSubject: subject\r\n"
        'Folder "Archive/2024"\r\n'
        'A second message_store has been found. Sorry, this must be an error.\r\n'
        'Folder "送信済みアイテム"\r\n'
    )

    assert parse_lspst_output(output) == [
        ArchiveFolder("受信トレイ", "受信トレイ", None),
        ArchiveFolder("Archive/2024", "Archive/2024", None),
        ArchiveFolder("送信済みアイテム", "送信済みアイテム", None),
    ]


def test_parser_ignores_malformed_folder_lines_and_accepts_bytes() -> None:
    output = b'Folder "Valid"\nFolder missing quotes\nFolder ""\n'

    assert LspstParser().parse(output) == [ArchiveFolder("Valid", "Valid", None)]


def test_detect_pst_format_uses_signature_and_version_word() -> None:
    ansi = b"!BDN" + b"\0" * 6 + struct.pack("<H", 14)
    unicode = b"!BDN" + b"\0" * 6 + struct.pack("<H", 23)

    assert detect_pst_format(ansi) == "ansi"
    assert detect_pst_type(unicode) == "unicode"


def test_detect_pst_format_returns_unknown_for_invalid_or_unsupported_input(
    tmp_path: Path,
) -> None:
    invalid = tmp_path / "invalid.pst"
    invalid.write_bytes(b"not a pst")
    unsupported = b"!BDN" + b"\0" * 6 + struct.pack("<H", 99)

    assert detect_pst_format(invalid) == "unknown"
    assert detect_pst_format(unsupported) == "unknown"
    assert detect_pst_format(tmp_path / "missing.pst") == "unknown"