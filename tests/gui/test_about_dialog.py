from __future__ import annotations

import json
from typing import Any

import pytest

from mail_dock.infrastructure.diagnostics import DiagnosticCheck, DiagnosticsReport
from mail_dock.presentation.views.dialogs import about_dialog

pytestmark = pytest.mark.gui


def test_about_dialog_displays_version_and_runs_diagnostics(qtbot: Any, monkeypatch: Any) -> None:
    dialog = about_dialog.AboutDialog()
    qtbot.addWidget(dialog)
    monkeypatch.setattr(
        about_dialog,
        "run_self_check",
        lambda extra_checks=(): DiagnosticsReport(
            "0.1.0", (DiagnosticCheck("version", True, "0.1.0"), *extra_checks)
        ),
    )
    monkeypatch.setattr(
        about_dialog,
        "qt_webengine_check",
        lambda: DiagnosticCheck("qt_webengine", True, "available"),
    )

    assert dialog.windowTitle() == "バージョン情報"
    assert "0.1.0" in dialog.version_label.text()
    dialog.diagnostics_button.click()

    result = json.loads(dialog.results.toPlainText())
    assert result["passed"] is True
    assert {item["name"] for item in result["checks"]} == {"version", "qt_webengine"}
