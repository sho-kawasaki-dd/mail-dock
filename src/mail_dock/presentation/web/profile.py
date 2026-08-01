"""Create the isolated QtWebEngine profile used for mail rendering."""

from __future__ import annotations

from PySide6.QtCore import QObject
from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEngineSettings

from .interceptor import MailUrlRequestInterceptor

_DISABLED_ATTRIBUTES = (
    QWebEngineSettings.WebAttribute.JavascriptEnabled,
    QWebEngineSettings.WebAttribute.LocalStorageEnabled,
    QWebEngineSettings.WebAttribute.PluginsEnabled,
    QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls,
    QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls,
    QWebEngineSettings.WebAttribute.AllowRunningInsecureContent,
    QWebEngineSettings.WebAttribute.ScreenCaptureEnabled,
    QWebEngineSettings.WebAttribute.FullScreenSupportEnabled,
    QWebEngineSettings.WebAttribute.AutoLoadIconsForPage,
)


def create_mail_profile(
    parent: QObject | None = None,
    *,
    request_interceptor: MailUrlRequestInterceptor | None = None,
) -> QWebEngineProfile:
    """Return an off-the-record profile configured for untrusted mail HTML.

    Pass the application object as ``parent`` so Qt retains the profile for
    the application lifetime. It must outlive every ``QWebEnginePage`` made
    from it.
    """
    profile = QWebEngineProfile(parent)
    if not profile.isOffTheRecord():
        profile.deleteLater()
        raise RuntimeError("Mail rendering requires an off-the-record profile")

    profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.NoCache)
    profile.setPersistentCookiesPolicy(
        QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies
    )
    settings = profile.settings()
    for attribute in _DISABLED_ATTRIBUTES:
        settings.setAttribute(attribute, False)

    interceptor = request_interceptor or MailUrlRequestInterceptor(profile)
    profile.setUrlRequestInterceptor(interceptor)
    return profile