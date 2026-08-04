from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtWebEngineCore import (
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineSettings,
    QWebEngineUrlRequestInfo,
)

from mail_dock.domain.messages import MessagePart, RenderedMessage
from mail_dock.presentation.web.interceptor import MailUrlRequestInterceptor
from mail_dock.presentation.web.page import MailPage
from mail_dock.presentation.web.profile import create_mail_profile
from mail_dock.presentation.web.schemes import CidSchemeHandler, MailBodySchemeHandler

pytestmark = pytest.mark.gui


@dataclass
class _RequestInfo:
    url: str
    resource_type: Any
    blocked: bool | None = None

    def requestUrl(self) -> QUrl:  # noqa: N802
        return QUrl(self.url)

    def resourceType(self) -> Any:  # noqa: N802
        return self.resource_type

    def block(self, blocked: bool) -> None:
        self.blocked = blocked


def test_profile_is_off_record_and_disables_persistence_and_dangerous_settings(
    qtbot: object,
) -> None:
    del qtbot
    profile = create_mail_profile()

    assert profile.isOffTheRecord()
    assert profile.httpCacheType() == QWebEngineProfile.HttpCacheType.NoCache
    assert (
        profile.persistentCookiesPolicy()
        == QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies
    )
    for attribute in (
        QWebEngineSettings.WebAttribute.JavascriptEnabled,
        QWebEngineSettings.WebAttribute.LocalStorageEnabled,
        QWebEngineSettings.WebAttribute.PluginsEnabled,
        QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls,
        QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls,
        QWebEngineSettings.WebAttribute.AllowRunningInsecureContent,
        QWebEngineSettings.WebAttribute.ScreenCaptureEnabled,
        QWebEngineSettings.WebAttribute.FullScreenSupportEnabled,
        QWebEngineSettings.WebAttribute.AutoLoadIconsForPage,
    ):
        assert not profile.settings().testAttribute(attribute)
    profile.deleteLater()


def test_interceptor_allows_mail_schemes_and_only_enabled_remote_images(qtbot: object) -> None:
    del qtbot
    interceptor = MailUrlRequestInterceptor()
    image_type = QWebEngineUrlRequestInfo.ResourceType.ResourceTypeImage
    requests = [
        _RequestInfo("cid:part", image_type),
        _RequestInfo("maildock:/body", image_type),
        _RequestInfo("http://example.invalid/image", image_type),
        _RequestInfo("file:///tmp/mail.eml", image_type),
    ]

    for request in requests:
        interceptor.interceptRequest(request)  # type: ignore[arg-type]
    assert [request.blocked for request in requests] == [False, False, True, True]

    interceptor.set_allow_remote_images(True)
    remote_image = _RequestInfo("https://example.invalid/image", image_type)
    remote_document = _RequestInfo("https://example.invalid/document", object())
    interceptor.interceptRequest(remote_image)  # type: ignore[arg-type]
    interceptor.interceptRequest(remote_document)  # type: ignore[arg-type]
    assert remote_image.blocked is False
    assert remote_document.blocked is True

    interceptor.reset_for_message()
    reset_request = _RequestInfo("https://example.invalid/image", image_type)
    interceptor.interceptRequest(reset_request)  # type: ignore[arg-type]
    assert reset_request.blocked is True


def test_unknown_cid_and_body_paths_are_rejected(qtbot: object) -> None:
    del qtbot

    class _Job:
        def __init__(self, url: str) -> None:
            self.url = QUrl(url)
            self.failed: object | None = None

        def requestUrl(self) -> QUrl:  # noqa: N802
            return self.url

        def fail(self, error: object) -> None:
            self.failed = error

    cid_job = _Job("cid:missing")
    body_job = _Job("maildock:/not-body")
    CidSchemeHandler().requestStarted(cid_job)  # type: ignore[arg-type]
    MailBodySchemeHandler().requestStarted(body_job)  # type: ignore[arg-type]
    assert cid_job.failed is not None
    assert body_job.failed is not None


def test_large_maildock_body_loads_and_cid_part_is_available(qtbot: Any) -> None:
    profile = create_mail_profile()
    body_handler = cast(MailBodySchemeHandler, profile.urlSchemeHandler(b"maildock"))
    cid_handler = cast(CidSchemeHandler, profile.urlSchemeHandler(b"cid"))
    rendered = RenderedMessage(
        html_body="<html><body>" + ("mail body " * 250_000) + "</body></html>",
        text_body="",
        parts=(MessagePart("logo", "image/png", None, b"PNG", True),),
    )
    body_handler.set_message(rendered)
    cid_handler.set_message(rendered)
    page = QWebEnginePage(profile)
    loaded: list[bool] = []
    html: list[str] = []
    page.loadFinished.connect(loaded.append)
    page.load(QUrl("maildock:/body"))
    qtbot.waitUntil(lambda: bool(loaded), timeout=5_000)
    page.toHtml(html.append)
    qtbot.waitUntil(lambda: bool(html), timeout=5_000)

    assert loaded == [True]
    assert len(html[0]) > 2_000_000
    page.deleteLater()
    profile.deleteLater()


def test_navigation_requires_one_body_load_and_confirms_allowed_external_links(
    qtbot: object,
) -> None:
    del qtbot
    profile = create_mail_profile()
    confirmed: list[str] = []
    opened: list[QUrl] = []

    def confirm_external_url(url: str) -> bool:
        confirmed.append(url)
        return True

    def open_external_url(url: QUrl) -> bool:
        opened.append(url)
        return True

    page = MailPage(
        profile,
        confirm_external_url=confirm_external_url,
        open_external_url=open_external_url,
    )

    assert page.acceptNavigationRequest(
        QUrl("maildock:/body"), QWebEnginePage.NavigationType.NavigationTypeOther, True
    )
    assert not page.acceptNavigationRequest(
        QUrl("maildock:/body"), QWebEnginePage.NavigationType.NavigationTypeOther, True
    )
    assert not page.acceptNavigationRequest(
        QUrl("https://example.com/mail"),
        QWebEnginePage.NavigationType.NavigationTypeLinkClicked,
        True,
    )
    assert confirmed == ["https://example.com/mail"]
    assert [url.toString() for url in opened] == ["https://example.com/mail"]
    assert not page.acceptNavigationRequest(
        QUrl("javascript:alert(1)"),
        QWebEnginePage.NavigationType.NavigationTypeLinkClicked,
        True,
    )
    assert confirmed == ["https://example.com/mail"]
    page.deleteLater()
    profile.deleteLater()
