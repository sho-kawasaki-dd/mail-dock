"""Presentation-layer diagnostics for Qt runtime components."""

from __future__ import annotations

from importlib import import_module

from mail_dock.infrastructure.diagnostics import DiagnosticCheck


def qt_webengine_check() -> DiagnosticCheck:
    """Return whether the Qt WebEngine runtime can be imported."""

    try:
        import_module("PySide6.QtWebEngineCore")
    except Exception as error:
        return DiagnosticCheck("qt_webengine", False, f"Unavailable ({type(error).__name__})")
    return DiagnosticCheck("qt_webengine", True, "PySide6.QtWebEngineCore is available")