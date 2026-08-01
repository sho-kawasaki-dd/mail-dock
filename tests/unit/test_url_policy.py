from __future__ import annotations

import pytest

from mail_dock.presentation.web.url_policy import is_allowed_external_url


@pytest.mark.parametrize(
    "url",
    (
        "https://example.com/",
        "http://example.com/path?q=mail",
        "HTTPS://example.com/encoded%20path",
    ),
)
def test_http_and_https_urls_are_allowed(url: str) -> None:
    assert is_allowed_external_url(url)


@pytest.mark.parametrize(
    "url",
    (
        "file:///tmp/message.eml",
        "javascript:alert(1)",
        "data:text/html,mail",
        "custom://example.com/mail",
        "https://user:password@example.com/",
        "https://example.com\n/",
        "https://example.com/\u202e",
        "https://example.com/" + "a" * 4097,
        "https://example.com:invalid/",
        "https://",
    ),
)
def test_unsafe_or_malformed_urls_are_rejected(url: str) -> None:
    assert not is_allowed_external_url(url)
