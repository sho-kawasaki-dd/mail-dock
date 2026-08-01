"""Registration of the private schemes used to render mail content."""

from __future__ import annotations

_registered = False


def register_schemes() -> None:
    """Register mail-dock schemes before the QApplication is constructed."""

    global _registered
    if _registered:
        return

    from PySide6.QtWebEngineCore import QWebEngineUrlScheme

    for name in (b"cid", b"maildock"):
        scheme = QWebEngineUrlScheme(name)
        scheme.setSyntax(QWebEngineUrlScheme.Syntax.Path)
        scheme.setFlags(
            QWebEngineUrlScheme.Flag.SecureScheme
            | QWebEngineUrlScheme.Flag.LocalScheme
            | QWebEngineUrlScheme.Flag.LocalAccessAllowed
        )
        QWebEngineUrlScheme.registerScheme(scheme)
    _registered = True
