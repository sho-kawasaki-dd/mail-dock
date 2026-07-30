"""Sanitize attachment names before a future attachment-save operation.

This module intentionally contains pure functions only. Actual file saving is
owned by Phase 3, where ``resolve_within`` must be called immediately before
the destination is opened.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Final

MAX_FILENAME_BYTES: Final = 255
FALLBACK_ATTACHMENT_NAME: Final = "attachment"

_PATH_SEPARATOR: Final[re.Pattern[str]] = re.compile(r"[/\\]")
_FORBIDDEN_CHARACTER: Final[re.Pattern[str]] = re.compile(r'[<>:"/\\|?*]')
_EXECUTABLE_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {".exe", ".scr", ".js", ".vbs", ".lnk", ".bat", ".cmd", ".ps1"}
)
_RESERVED_NAMES: Final[re.Pattern[str]] = re.compile(
    r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])$", re.IGNORECASE
)


@dataclass(frozen=True)
class SanitizedName:
    """The safe attachment name and warnings raised while deriving it."""

    name: str
    warnings: tuple[str, ...] = ()

    @property
    def filename(self) -> str:
        """Return the sanitized filename using an explicit filename alias."""

        return self.name

    @property
    def warning(self) -> bool:
        """Whether any sanitization or security warning was raised."""

        return bool(self.warnings)

    @property
    def is_executable(self) -> bool:
        """Whether the resulting filename has an executable extension."""

        return "executable_extension" in self.warnings

    @property
    def executable_warning(self) -> bool:
        """Alias for the executable-extension warning flag."""

        return self.is_executable


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value

    result: list[str] = []
    used_bytes = 0
    for character in value:
        character_bytes = len(character.encode("utf-8"))
        if used_bytes + character_bytes > max_bytes:
            break
        result.append(character)
        used_bytes += character_bytes
    return "".join(result)


def _truncate_preserving_extension(value: str, max_bytes: int) -> str:
    if len(value.encode("utf-8")) <= max_bytes:
        return value

    if "." not in value or value.startswith("."):
        return _truncate_utf8(value, max_bytes)

    stem, extension = value.rsplit(".", 1)
    extension = f".{extension}"
    extension_bytes = len(extension.encode("utf-8"))
    if extension_bytes >= max_bytes:
        return _truncate_utf8(extension, max_bytes)
    return _truncate_utf8(stem, max_bytes - extension_bytes) + extension


def _extension(value: str) -> str:
    if "." not in value or value.startswith("."):
        return ""
    return f".{value.rsplit('.', 1)[1].casefold()}"


def sanitize_attachment_name(name: str) -> SanitizedName:
    """Return a safe, bounded attachment filename and security warnings.

    Path components are reduced to the final component, forbidden and control
    characters become underscores, and Windows-reserved basenames receive a
    suffix. The result is NFC-normalized and limited to 255 UTF-8 bytes while
    preserving its final extension where possible.
    """

    warnings: list[str] = []
    value = unicodedata.normalize("NFC", name)

    components = _PATH_SEPARATOR.split(value)
    if len(components) > 1:
        warnings.append("path_component")
    value = components[-1]

    replaced = []
    replaced_forbidden = False
    replaced_control = False
    for character in value:
        if _FORBIDDEN_CHARACTER.fullmatch(character):
            replaced.append("_")
            replaced_forbidden = True
        elif unicodedata.category(character) == "Cc":
            replaced.append("_")
            replaced_control = True
        else:
            replaced.append(character)
    value = "".join(replaced)
    if replaced_forbidden:
        warnings.append("forbidden_character")
    if replaced_control:
        warnings.append("control_character")

    stripped_value = value.rstrip(". ")
    if stripped_value != value:
        warnings.append("trailing_character")
    value = stripped_value

    if not value:
        value = FALLBACK_ATTACHMENT_NAME
        warnings.append("empty_name")

    basename = value.split(".", 1)[0]
    if _RESERVED_NAMES.fullmatch(basename):
        value = f"{basename}_{value[len(basename) :]}"
        warnings.append("reserved_name")

    value = unicodedata.normalize("NFC", value)
    bounded_value = _truncate_preserving_extension(value, MAX_FILENAME_BYTES)
    if bounded_value != value:
        warnings.append("truncated")
    value = bounded_value

    if _extension(value) in _EXECUTABLE_EXTENSIONS:
        warnings.append("executable_extension")

    return SanitizedName(value, tuple(warnings))


def resolve_within(base: Path, name: str) -> Path:
    """Resolve ``name`` and reject destinations outside the resolved base."""

    resolved_base = base.resolve()
    destination = (resolved_base / Path(name)).resolve()
    try:
        destination.relative_to(resolved_base)
    except ValueError as error:
        raise ValueError("attachment destination escapes its base directory") from error
    if destination == resolved_base:
        raise ValueError("attachment destination escapes or equals its base directory")
    return destination
