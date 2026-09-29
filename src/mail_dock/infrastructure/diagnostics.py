"""Read-only checks for a mail-dock runtime installation."""

from __future__ import annotations

import codecs
import os
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources

from mail_dock import __version__
from mail_dock.infrastructure import app_paths
from mail_dock.infrastructure.importers.readpst_locator import ReadPstLocator
from mail_dock.infrastructure.security.keyring_store import (
    KeyringBackendStatus,
    detect_backend,
)


@dataclass(frozen=True)
class DiagnosticCheck:
    """A single safe-to-display diagnostic result."""

    name: str
    passed: bool
    detail: str

    def as_dict(self) -> dict[str, str | bool]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class DiagnosticsReport:
    """The version and results of a self-check run."""

    version: str
    checks: tuple[DiagnosticCheck, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def as_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "passed": self.passed,
            "checks": [check.as_dict() for check in self.checks],
        }


def _migration_check() -> DiagnosticCheck:
    try:
        migration_files = sorted(
            item.name
            for item in resources.files("mail_dock").joinpath("migrations").iterdir()
            if item.name.endswith(".sql") and item.is_file()
        )
    except Exception as error:
        return DiagnosticCheck(
            "migrations", False, f"Could not enumerate ({type(error).__name__})"
        )
    if not migration_files:
        return DiagnosticCheck("migrations", False, "No migration SQL files found")
    return DiagnosticCheck("migrations", True, f"{len(migration_files)} SQL files available")


def _fts5_check() -> DiagnosticCheck:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(":memory:")
        connection.execute(
            "CREATE VIRTUAL TABLE diagnostic_fts USING fts5(value, tokenize='trigram')"
        )
        connection.execute("INSERT INTO diagnostic_fts(value) VALUES ('mail-dock')")
        connection.execute(
            "SELECT value FROM diagnostic_fts WHERE diagnostic_fts MATCH 'mail'"
        )
    except sqlite3.Error as error:
        return DiagnosticCheck(
            "sqlite_fts5_trigram", False, f"Unavailable ({type(error).__name__})"
        )
    finally:
        if connection is not None:
            connection.close()
    return DiagnosticCheck("sqlite_fts5_trigram", True, "FTS5 trigram is available")


def _codec_available(name: str) -> bool:
    try:
        codecs.lookup(name)
    except LookupError:
        return False
    return True


def _codec_check() -> DiagnosticCheck:
    codecs_to_check = ("iso2022_jp_ext", "cp932", "euc_jp")
    unavailable = [name for name in codecs_to_check if not _codec_available(name)]
    if unavailable:
        return DiagnosticCheck("mail_codecs", False, f"Unavailable: {', '.join(unavailable)}")
    return DiagnosticCheck("mail_codecs", True, f"Available: {', '.join(codecs_to_check)}")


def _readpst_check() -> DiagnosticCheck:
    try:
        version = ReadPstLocator().get_version()
    except Exception as error:
        return DiagnosticCheck("readpst", False, f"Unavailable ({type(error).__name__})")
    return DiagnosticCheck("readpst", True, version)


def _keyring_check() -> DiagnosticCheck:
    try:
        status = detect_backend()
    except Exception as error:
        return DiagnosticCheck(
            "keyring", True, f"Unavailable ({type(error).__name__}); session-only mode"
        )
    return DiagnosticCheck("keyring", True, status.value)


def required_keyring_check() -> DiagnosticCheck:
    """Require the approved Windows backend for release smoke tests."""

    if os.name != "nt":
        return DiagnosticCheck("required_keyring", True, "Windows backend check not applicable")
    try:
        status = detect_backend()
    except Exception as error:
        return DiagnosticCheck("required_keyring", False, f"Unavailable ({type(error).__name__})")
    passed = status is KeyringBackendStatus.SUPPORTED
    detail = status.value if passed else f"Expected supported Windows backend; found {status.value}"
    return DiagnosticCheck("required_keyring", passed, detail)


def _bundled_licenses_check() -> DiagnosticCheck:
    root = app_paths.bundle_root()
    required_files = (
        root / "vendor" / "readpst" / "COPYING",
        root / "licenses" / "QT-SOURCE.md",
        root / "licenses" / "qtwebengine" / "LICENSE.Chromium",
        root / "licenses" / "qtwebengine" / "src" / "3rdparty" / "chromium" / "LICENSE",
    )
    missing = [
        path.relative_to(root).as_posix() for path in required_files if not path.is_file()
    ]
    if missing:
        return DiagnosticCheck("bundled_licenses", False, f"Missing: {', '.join(missing)}")
    return DiagnosticCheck(
        "bundled_licenses", True, "Required bundled license materials are present"
    )


def run_self_check(
    extra_checks: Sequence[DiagnosticCheck] = (),
) -> DiagnosticsReport:
    """Run read-only checks without loading settings or opening the storage root."""

    checks = [
        DiagnosticCheck("version", True, __version__),
        _migration_check(),
        _fts5_check(),
        _codec_check(),
        _readpst_check(),
        _keyring_check(),
    ]
    if app_paths.is_frozen():
        checks.append(_bundled_licenses_check())
    checks.extend(extra_checks)
    return DiagnosticsReport(__version__, tuple(checks))