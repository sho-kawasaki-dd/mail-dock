from __future__ import annotations

import json
import os
import sqlite3
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

import mail_dock.__main__ as cli
import mail_dock.config as app_config
import mail_dock.infrastructure.app_paths as app_paths
import mail_dock.infrastructure.diagnostics as diagnostics
import mail_dock.infrastructure.importers.readpst_locator as readpst_locator
from mail_dock.infrastructure.diagnostics import DiagnosticCheck
from mail_dock.infrastructure.security import keyring_store
from mail_dock.infrastructure.security.keyring_store import KeyringBackendStatus
from mail_dock.presentation import diagnostics_qt


def test_self_check_reports_runtime_components_without_secrets(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        readpst_locator.ReadPstLocator, "get_version", lambda _self: "readpst 0.6.76"
    )
    monkeypatch.setattr(keyring_store, "detect_backend", lambda: KeyringBackendStatus.UNSUPPORTED)

    report = diagnostics.run_self_check()

    assert report.passed
    assert {check.name for check in report.checks} >= {
        "version",
        "migrations",
        "sqlite_fts5_trigram",
        "mail_codecs",
        "readpst",
        "keyring",
    }
    serialized = json.dumps(report.as_dict())
    assert "password" not in serialized
    assert "token" not in serialized


def test_self_check_reports_readpst_failure(monkeypatch: Any) -> None:
    def fail_version(_self: object) -> str:
        raise RuntimeError("private path")

    monkeypatch.setattr(readpst_locator.ReadPstLocator, "get_version", fail_version)

    report = diagnostics.run_self_check()

    readpst = next(check for check in report.checks if check.name == "readpst")
    assert not readpst.passed
    assert "private path" not in readpst.detail


def test_self_check_reports_migration_enumeration_failure(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        resources,
        "files",
        lambda _package: (_ for _ in ()).throw(OSError("private path")),
    )

    check = diagnostics._migration_check()

    assert not check.passed
    assert "private path" not in check.detail


def test_self_check_reports_fts5_failure(monkeypatch: Any) -> None:
    def fail_connect(_database: str) -> object:
        raise sqlite3.OperationalError("private database path")

    monkeypatch.setattr(sqlite3, "connect", fail_connect)

    check = diagnostics._fts5_check()

    assert not check.passed
    assert "private database path" not in check.detail


def test_self_check_reports_missing_codec(monkeypatch: Any) -> None:
    monkeypatch.setattr(diagnostics, "_codec_available", lambda name: name != "cp932")

    check = diagnostics._codec_check()

    assert not check.passed
    assert "cp932" in check.detail


def test_qt_webengine_check_reports_import_failure(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        diagnostics_qt,
        "import_module",
        lambda _name: (_ for _ in ()).throw(ImportError("private path")),
    )

    check = diagnostics_qt.qt_webengine_check()

    assert not check.passed
    assert "private path" not in check.detail


def test_qt_webengine_check_reports_import_success(monkeypatch: Any) -> None:
    monkeypatch.setattr(diagnostics_qt, "import_module", lambda _name: object())

    check = diagnostics_qt.qt_webengine_check()

    assert check.passed


def test_frozen_license_check_accepts_required_materials(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setattr(app_paths, "bundle_root", lambda: tmp_path)
    required_files = (
        tmp_path / "vendor" / "readpst" / "COPYING",
        tmp_path / "licenses" / "QT-SOURCE.md",
        tmp_path / "licenses" / "qtwebengine" / "LICENSE.Chromium",
        tmp_path / "licenses" / "qtwebengine" / "src" / "3rdparty" / "chromium" / "LICENSE",
    )
    for path in required_files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("license", encoding="utf-8")

    check = diagnostics._bundled_licenses_check()

    assert check.passed


def test_extra_failed_check_fails_report(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        readpst_locator.ReadPstLocator, "get_version", lambda _self: "readpst 0.6.76"
    )

    report = diagnostics.run_self_check(
        extra_checks=(DiagnosticCheck("qt_webengine", False, "unavailable"),)
    )

    assert not report.passed


def test_self_check_reports_missing_frozen_license_material(
    monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setattr(app_paths, "is_frozen", lambda: True)
    monkeypatch.setattr(app_paths, "bundle_root", lambda: tmp_path)
    monkeypatch.setattr(
        readpst_locator.ReadPstLocator, "get_version", lambda _self: "readpst 0.6.76"
    )

    report = diagnostics.run_self_check()

    license_check = next(check for check in report.checks if check.name == "bundled_licenses")
    assert not license_check.passed
    assert "QT-SOURCE.md" in license_check.detail


@pytest.mark.parametrize(
    ("status", "passed"),
    [
        (KeyringBackendStatus.SUPPORTED, True),
        (KeyringBackendStatus.UNSUPPORTED, False),
        (KeyringBackendStatus.UNAVAILABLE, False),
    ],
)
def test_required_keyring_check_on_windows(
    monkeypatch: Any,
    status: KeyringBackendStatus,
    passed: bool,
) -> None:
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(keyring_store, "detect_backend", lambda: status)

    check = diagnostics.required_keyring_check()

    assert check.passed is passed


def test_self_check_cli_does_not_load_configuration(monkeypatch: Any, tmp_path: Path) -> None:
    output_path = tmp_path / "diagnostics.json"

    def config_must_not_load() -> object:
        raise AssertionError("self-check must not load config")

    monkeypatch.setattr(app_config, "load", config_must_not_load)
    monkeypatch.setattr(app_paths, "is_frozen", lambda: False)
    monkeypatch.setattr(
        cli,
        "run_self_check",
        lambda extra_checks=(): diagnostics.DiagnosticsReport(
            "0.1.0", (DiagnosticCheck("test", True, "ok"), *extra_checks)
        ),
    )
    monkeypatch.setattr(
        "mail_dock.presentation.diagnostics_qt.qt_webengine_check",
        lambda: DiagnosticCheck("qt_webengine", True, "available"),
    )

    result = cli.main(["self-check", "--output", str(output_path)])

    assert result == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["passed"] is True
    assert {item["name"] for item in payload["checks"]} == {"test", "qt_webengine"}


def test_self_check_cli_returns_failure_for_failed_check(monkeypatch: Any, tmp_path: Path) -> None:
    output_path = tmp_path / "diagnostics.json"
    monkeypatch.setattr(app_paths, "is_frozen", lambda: False)
    monkeypatch.setattr(
        cli,
        "run_self_check",
        lambda extra_checks=(): diagnostics.DiagnosticsReport(
            "0.1.0", (DiagnosticCheck("test", False, "failed"), *extra_checks)
        ),
    )
    monkeypatch.setattr(
        "mail_dock.presentation.diagnostics_qt.qt_webengine_check",
        lambda: DiagnosticCheck("qt_webengine", True, "available"),
    )

    result = cli.main(["self-check", "--output", str(output_path)])

    assert result == 1


def test_self_check_reports_missing_dynamic_module(monkeypatch: Any) -> None:
    def fail_import(name: str) -> object:
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(diagnostics, "import_module", fail_import)

    check = diagnostics._dynamic_modules_check()

    assert not check.passed
    assert "html_sanitizer" in check.detail
