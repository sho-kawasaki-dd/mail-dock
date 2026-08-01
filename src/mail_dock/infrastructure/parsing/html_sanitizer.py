"""Sanitize untrusted HTML mail bodies before they reach the web view."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup
from bs4.element import Tag

_REMOVED_TAGS = (
    "script",
    "iframe",
    "frame",
    "object",
    "embed",
    "form",
    "link",
    "base",
)
_DANGEROUS_URL_RE = re.compile(r"[\x00-\x20]+")
_CSP_HTTP_EQUIV = "content-security-policy"
_REFRESH_HTTP_EQUIV = "refresh"


def _attribute_text(tag: Tag, name: str) -> str:
    value = tag.get(name, "")
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    return str(value)


def _is_dangerous_url(value: object) -> bool:
    normalized = _DANGEROUS_URL_RE.sub("", str(value)).casefold()
    return normalized.startswith(("javascript:", "data:"))


def _direct_child(parent: Tag, name: str) -> Tag | None:
    for child in parent.children:
        if isinstance(child, Tag) and child.name == name:
            return child
    return None


def _remove_unsafe_content(soup: BeautifulSoup) -> None:
    for element in soup.find_all(_REMOVED_TAGS):
        element.decompose()

    for meta in soup.find_all("meta"):
        http_equiv = _attribute_text(meta, "http-equiv").casefold()
        if http_equiv in {_CSP_HTTP_EQUIV, _REFRESH_HTTP_EQUIV}:
            meta.decompose()

    for tag in soup.find_all(True):
        for attribute in list(tag.attrs):
            normalized_attribute = attribute.casefold()
            if normalized_attribute.startswith("on"):
                del tag.attrs[attribute]
                continue
            if normalized_attribute in {"href", "src"} and _is_dangerous_url(tag.attrs[attribute]):
                del tag.attrs[attribute]


def _ensure_document_structure(soup: BeautifulSoup) -> tuple[Tag, Tag]:
    root = next(
        (child for child in soup.contents if isinstance(child, Tag) and child.name == "html"),
        None,
    )
    if root is None:
        root = soup.new_tag("html")
        document_head = soup.new_tag("head")
        document_body = soup.new_tag("body")
        for child in list(soup.contents):
            if isinstance(child, Tag) and child.name == "head":
                for head_child in list(child.contents):
                    document_head.append(head_child.extract())
                child.decompose()
            elif isinstance(child, Tag) and child.name == "body":
                for body_child in list(child.contents):
                    document_body.append(body_child.extract())
                child.decompose()
            else:
                document_body.append(child.extract())
        root.append(document_head)
        root.append(document_body)
        soup.append(root)
        return root, document_head

    head: Tag | None = _direct_child(root, "head")
    if head is None:
        head = soup.new_tag("head")
        root.insert(0, head)

    body: Tag | None = _direct_child(root, "body")
    if body is None:
        body = soup.new_tag("body")
        for child in list(root.contents):
            if child is not head:
                body.append(child.extract())
        root.append(body)

    return root, head


def sanitize_mail_html(html: str, *, allow_remote_images: bool) -> str:
    """Return HTML with active content removed and a restrictive CSP enforced."""
    soup = BeautifulSoup(html, "html.parser")
    _remove_unsafe_content(soup)
    _, head = _ensure_document_structure(soup)

    image_sources = "cid: https: http:" if allow_remote_images else "cid:"
    csp = (
        "default-src 'none'; "
        f"img-src {image_sources}; "
        "style-src 'unsafe-inline'; "
        "form-action 'none'; "
        "frame-src 'none'"
    )
    csp_meta = soup.new_tag(
        "meta",
        attrs={"http-equiv": "Content-Security-Policy", "content": csp},
    )
    head.insert(0, csp_meta)
    return str(soup)
