"""Best-effort parsing helpers for the optional ``lspst`` inspection tool."""

from __future__ import annotations

import re
import struct
from pathlib import Path
from typing import Literal

from mail_dock.domain.importer import ArchiveFolder

PstFormat = Literal["unicode", "ansi", "unknown"]

_FOLDER_LINE = re.compile(r'^Folder[ \t]+"(?P<name>.*)"[ \t]*$')
_PST_SIGNATURE = b"!BDN"
_PST_VERSION_OFFSET = 10
_PST_VERSION_SIZE = 2
_ANSI_VERSIONS = frozenset({14})
_UNICODE_VERSIONS = frozenset({15, 23, 24})


class LspstParser:
    """Parse only the stable-enough subset of ``lspst`` output.

    ``lspst`` output is intentionally treated as advisory.  In particular,
    its exit status and ``Email`` lines are not part of this parser's input
    contract because valid PSTs can produce partial output and misleading
    counts.
    """

    def parse(self, output: str | bytes) -> list[ArchiveFolder]:
        """Return flat folder references found in ``lspst`` output.

        Every returned folder has an unknown message count.  Malformed lines,
        diagnostics, and non-folder records are ignored.
        """

        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")

        folders: list[ArchiveFolder] = []
        for line in output.splitlines():
            match = _FOLDER_LINE.fullmatch(line)
            if match is None:
                continue
            name = match.group("name")
            if not name:
                continue
            folders.append(
                ArchiveFolder(
                    relative_path=name,
                    display_name=name,
                    estimated_count=None,
                )
            )
        return folders

    def parse_file(self, source: Path) -> list[ArchiveFolder]:
        """Parse a UTF-8 ``lspst`` output file, returning no folders on read failure."""

        try:
            output = source.read_bytes()
        except OSError:
            return []
        return self.parse(output)


def parse_lspst_output(output: str | bytes) -> list[ArchiveFolder]:
    """Parse advisory folder records without using an ``lspst`` exit code."""

    return LspstParser().parse(output)


def detect_pst_format(source: Path | bytes | bytearray) -> PstFormat:
    """Classify a PST from its signature and little-endian version word.

    The version word is at offset 10 in the PST header.  This function never
    guesses from the file extension and returns ``"unknown"`` for malformed,
    unsupported, or unreadable input.
    """

    try:
        header = bytes(source) if isinstance(source, (bytes, bytearray)) else source.read_bytes()
    except OSError:
        return "unknown"

    version_end = _PST_VERSION_OFFSET + _PST_VERSION_SIZE
    if len(header) < version_end or header[: len(_PST_SIGNATURE)] != _PST_SIGNATURE:
        return "unknown"

    version = struct.unpack_from("<H", header, _PST_VERSION_OFFSET)[0]
    if version in _UNICODE_VERSIONS:
        return "unicode"
    if version in _ANSI_VERSIONS:
        return "ansi"
    return "unknown"


def detect_pst_type(source: Path | bytes | bytearray) -> PstFormat:
    """Compatibility name for :func:`detect_pst_format`."""

    return detect_pst_format(source)


__all__ = [
    "LspstParser",
    "PstFormat",
    "detect_pst_format",
    "detect_pst_type",
    "parse_lspst_output",
]