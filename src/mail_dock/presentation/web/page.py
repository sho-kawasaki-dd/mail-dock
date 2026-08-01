"""Navigation policy for the untrusted mail preview page."""

from __future__ import annotations

from collections.abc import Callable
from typing import override

from PySide6.QtCore import QObject, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtNetwork import QAuthenticator
from PySide6.QtWebEngineCore import (
    QWebEngineCertificateError,
    QWebEnginePage,
    QWebEngineProfile,
)
from PySide6.QtWidgets import QMessageBox, QWidget

from mail_dock.presentation import strings

from .url_policy import is_allowed_external_url

ConfirmExternalUrl = Callable[[str], bool]
OpenExternalUrl = Callable[[QUrl], bool]


def _confirm_external_url(url: str, parent: QWidget | None) -> bool:
    """Ask for confirmation before handing a URL to the operating system."""
    result = QMessageBox.question(
        parent,
        strings.DIALOG_CONFIRM_TITLE,
        strings.DIALOG_CONFIRM_EXTERNAL_LINK.format(url=url),
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return result == QMessageBox.StandardButton.Yes


class MailPage(QWebEnginePage):
    """Render mail content while keeping navigation outside the application."""

    def __init__(
        self,
        profile: QWebEngineProfile,
        parent: QObject | None = None,
        *,
        dialog_parent: QWidget | None = None,
        confirm_external_url: ConfirmExternalUrl | None = None,
        open_external_url: OpenExternalUrl | None = None,
    ) -> None:
        super().__init__(profile, parent)
        self._body_navigation_allowed = True
        self._dialog_parent = dialog_parent
        self._confirm_external_url = confirm_external_url or (
            lambda url: _confirm_external_url(url, self._dialog_parent)
        )
        self._open_external_url = open_external_url or QDesktopServices.openUrl
        self.authenticationRequired.connect(self._reject_authentication)
        self.proxyAuthenticationRequired.connect(self._reject_proxy_authentication)
        self.certificateError.connect(self._reject_certificate_error)

    def reset_body_navigation(self) -> None:
        """Allow one new ``maildock:`` body navigation for the next message."""
        self._body_navigation_allowed = True

    @override
    def acceptNavigationRequest(
        self,
        url: QUrl | str,
        navigation_type: QWebEnginePage.NavigationType,
        is_main_frame: bool,
    ) -> bool:
        qt_url = QUrl(url) if isinstance(url, str) else url
        scheme = qt_url.scheme().casefold()
        if is_main_frame and scheme == "maildock":
            if not self._body_navigation_allowed:
                return False
            self._body_navigation_allowed = False
            return True

        if (
            not is_main_frame
            or navigation_type != QWebEnginePage.NavigationType.NavigationTypeLinkClicked
        ):
            return False

        external_url = qt_url.toString(QUrl.ComponentFormattingOption.FullyEncoded)
        if not is_allowed_external_url(external_url):
            return False
        if self._confirm_external_url(external_url):
            self._open_external_url(QUrl(external_url))
        return False

    @override
    def javaScriptAlert(self, security_origin: QUrl | str, message: str) -> None:
        del security_origin, message

    @override
    def javaScriptConfirm(self, security_origin: QUrl | str, message: str) -> bool:
        del security_origin, message
        return False

    @override
    def javaScriptPrompt(
        self,
        security_origin: QUrl | str,
        message: str,
        default_value: str,
    ) -> tuple[bool, str]:
        del security_origin, message, default_value
        return False, ""

    @staticmethod
    def _reject_certificate_error(error: QWebEngineCertificateError) -> None:
        error.rejectCertificate()

    @staticmethod
    def _reject_authentication(url: QUrl, authenticator: QAuthenticator) -> None:
        del url
        authenticator.setUser("")
        authenticator.setPassword("")

    @staticmethod
    def _reject_proxy_authentication(
        url: QUrl,
        authenticator: QAuthenticator,
        proxy_host: str,
    ) -> None:
        del url, proxy_host
        authenticator.setUser("")
        authenticator.setPassword("")
