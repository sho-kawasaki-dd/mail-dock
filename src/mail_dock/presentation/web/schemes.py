"""Registration of the private schemes used to render mail content."""

from __future__ import annotations

from typing import override
from urllib.parse import unquote

from PySide6.QtCore import QBuffer, QIODevice, QObject
from PySide6.QtWebEngineCore import (
    QWebEngineProfile,
    QWebEngineUrlRequestJob,
    QWebEngineUrlScheme,
    QWebEngineUrlSchemeHandler,
)

from mail_dock.domain.messages import MessagePart, RenderedMessage

_registered = False


def _request_identifier(job: QWebEngineUrlRequestJob) -> str:
    url = job.requestUrl()
    path = url.path().lstrip("/")
    if not path:
        path = url.host()
    return unquote(path)


def _reply_with_bytes(
    job: QWebEngineUrlRequestJob,
    content_type: str,
    payload: bytes,
) -> None:
    buffer = QBuffer(job)
    buffer.setData(payload)
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    job.reply(content_type.encode("ascii", errors="replace"), buffer)


class CidSchemeHandler(QWebEngineUrlSchemeHandler):
    """Serve inline MIME parts addressed by ``cid:`` URLs."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._parts: tuple[MessagePart, ...] = ()

    def set_message(self, message: RenderedMessage | None) -> None:
        """Replace the parts available to the currently displayed message."""
        self._parts = () if message is None else message.parts

    def clear_message(self) -> None:
        """Discard the previous message's parts."""
        self._parts = ()

    @override
    def requestStarted(self, job: QWebEngineUrlRequestJob) -> None:
        content_id = _request_identifier(job)
        part = next(
            (candidate for candidate in self._parts if candidate.content_id == content_id),
            None,
        )
        if part is None:
            job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
            return
        _reply_with_bytes(job, part.content_type, part.payload)


class MailBodySchemeHandler(QWebEngineUrlSchemeHandler):
    """Serve the current message body without using ``QWebEnginePage.setHtml``."""

    _BODY_PATHS = frozenset({"", "body", "message/body"})

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._html_body: str | None = None

    def set_message(self, message: RenderedMessage | None) -> None:
        """Replace the HTML body available to the currently displayed message."""
        self._html_body = None if message is None else message.html_body

    def clear_message(self) -> None:
        """Discard the previous message's body."""
        self._html_body = None

    @override
    def requestStarted(self, job: QWebEngineUrlRequestJob) -> None:
        path = _request_identifier(job)
        if path not in self._BODY_PATHS or self._html_body is None:
            job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
            return
        _reply_with_bytes(job, "text/html; charset=utf-8", self._html_body.encode("utf-8"))


def install_scheme_handlers(
    profile: QWebEngineProfile,
    *,
    cid_handler: CidSchemeHandler | None = None,
    body_handler: MailBodySchemeHandler | None = None,
) -> tuple[CidSchemeHandler, MailBodySchemeHandler]:
    """Install and return the handlers owned by a mail-rendering profile."""
    cid_handler = cid_handler or CidSchemeHandler(profile)
    body_handler = body_handler or MailBodySchemeHandler(profile)
    profile.installUrlSchemeHandler(b"cid", cid_handler)
    profile.installUrlSchemeHandler(b"maildock", body_handler)
    return cid_handler, body_handler


def register_schemes() -> None:
    """Register mail-dock schemes before the QApplication is constructed."""

    global _registered
    if _registered:
        return

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
