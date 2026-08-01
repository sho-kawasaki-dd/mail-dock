"""Filter network requests made while rendering untrusted mail HTML."""

from __future__ import annotations

from typing import override

from PySide6.QtCore import QObject
from PySide6.QtWebEngineCore import (
    QWebEngineUrlRequestInfo,
    QWebEngineUrlRequestInterceptor,
)

_ALLOWED_LOCAL_SCHEMES = frozenset({"cid", "maildock"})
_REMOTE_SCHEMES = frozenset({"http", "https"})
_IMAGE_RESOURCE_TYPE = QWebEngineUrlRequestInfo.ResourceType.ResourceTypeImage


class MailUrlRequestInterceptor(QWebEngineUrlRequestInterceptor):
    """Allow only mail content and explicitly enabled remote images.

    Remote image access is deliberately a transient flag. The controller that
    switches the displayed message must call ``reset_for_message`` before
    loading the next message so permission cannot carry over between messages.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._allow_remote_images = False

    @property
    def allow_remote_images(self) -> bool:
        """Whether HTTP(S) image requests are temporarily allowed."""
        return self._allow_remote_images

    def set_allow_remote_images(self, enabled: bool) -> None:
        """Set the per-message permission for HTTP(S) image requests."""
        self._allow_remote_images = enabled

    def reset_for_message(self) -> None:
        """Clear temporary remote-image permission for a new message."""
        self._allow_remote_images = False

    @override
    def interceptRequest(self, info: QWebEngineUrlRequestInfo) -> None:
        """Block every request except approved local schemes and image loads."""
        scheme = info.requestUrl().scheme().casefold()
        allowed = scheme in _ALLOWED_LOCAL_SCHEMES
        if (
            not allowed
            and self._allow_remote_images
            and scheme in _REMOTE_SCHEMES
            and info.resourceType() == _IMAGE_RESOURCE_TYPE
        ):
            allowed = True
        info.block(not allowed)
