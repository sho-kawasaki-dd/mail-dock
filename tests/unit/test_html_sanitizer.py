from __future__ import annotations

from bs4 import BeautifulSoup
from bs4.element import Tag

from mail_dock.infrastructure.parsing.html_sanitizer import (
    contains_remote_image_reference,
    sanitize_mail_html,
)


def _first_tag(soup: BeautifulSoup, name: str) -> Tag:
    element = soup.find(name)
    assert isinstance(element, Tag)
    return element


def test_sanitizer_removes_active_content_and_injects_csp() -> None:
    html = """
    <html><head>
      <meta http-equiv="Content-Security-Policy" content="default-src *">
      <meta http-equiv="refresh" content="0; url=https://attacker.test">
    </head><body>
      <script>window.secret = true;</script>
      <iframe src="https://attacker.test"></iframe>
      <object data="https://attacker.test"></object>
      <embed src="https://attacker.test">
      <form action="https://attacker.test"><input></form>
      <link rel="stylesheet" href="https://attacker.test/style.css">
      <base href="https://attacker.test/">
      <a onclick="steal()" href="javascript:alert(1)">bad</a>
      <img onerror="steal()" src="data:text/html,blocked">
    </body></html>
    """

    result = BeautifulSoup(sanitize_mail_html(html, allow_remote_images=False), "html.parser")

    assert len(result.find_all("meta", attrs={"http-equiv": "Content-Security-Policy"})) == 1
    assert result.find("meta", attrs={"http-equiv": "refresh"}) is None
    assert (
        result.find_all(["script", "iframe", "frame", "object", "embed", "form", "link", "base"])
        == []
    )
    assert _first_tag(result, "a").get("onclick") is None
    assert _first_tag(result, "a").get("href") is None
    assert _first_tag(result, "img").get("onerror") is None
    assert _first_tag(result, "img").get("src") is None

    csp = _first_tag(result, "meta").get("content")
    assert csp == (
        "default-src 'none'; img-src cid:; style-src 'unsafe-inline'; "
        "form-action 'none'; frame-src 'none'"
    )


def test_sanitizer_adds_document_structure_and_remote_image_sources() -> None:
    result = BeautifulSoup(
        sanitize_mail_html("<p>Hello</p>", allow_remote_images=True),
        "html.parser",
    )

    assert result.html is not None
    assert result.head is not None
    assert result.body is not None
    paragraph = _first_tag(result, "body").find("p")
    assert isinstance(paragraph, Tag)
    assert paragraph.get_text() == "Hello"
    meta = _first_tag(result, "head").find("meta")
    assert isinstance(meta, Tag)
    csp = meta.get("content")
    assert isinstance(csp, str)
    assert "img-src cid: https: http:" in csp


def test_sanitizer_handles_empty_and_malformed_html() -> None:
    for html in ("", "<div><p>unclosed"):
        result = BeautifulSoup(sanitize_mail_html(html, allow_remote_images=False), "html.parser")

        assert result.html is not None
        assert result.head is not None
        assert result.body is not None
        assert result.head.find("meta") is not None


def test_contains_remote_image_reference_detects_img_tag() -> None:
    assert contains_remote_image_reference('<img src="https://example.test/pixel.gif">')
    assert contains_remote_image_reference('<img src="HTTP://example.test/pixel.gif">')


def test_contains_remote_image_reference_detects_css_background() -> None:
    assert contains_remote_image_reference(
        '<div style="background-image: url(https://example.test/bg.png)"></div>'
    )
    assert contains_remote_image_reference(
        "<style>.x { background: url('https://example.test/bg.png'); }</style>"
    )


def test_contains_remote_image_reference_ignores_local_and_missing_images() -> None:
    assert not contains_remote_image_reference("<p>Hello</p>")
    assert not contains_remote_image_reference('<img src="cid:logo">')
    assert not contains_remote_image_reference('<img src="data:image/png;base64,AAA">')

