"""Convert HTML mail bodies into normalized text for search and full-text indexing."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Comment


def html_to_text(html: str) -> str:
    """Return visible HTML text with insignificant whitespace compressed."""
    soup = BeautifulSoup(html, "html.parser")

    for element in soup.find_all(("script", "style")):
        element.decompose()
    for comment in soup.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()

    # This is text extraction for search/full-text indexing only; the Phase 3
    # HTML display sandbox is responsible for rendering untrusted HTML.
    text = soup.get_text("\n")
    lines = [re.sub(r"[^\S\r\n]+", " ", line).strip() for line in text.splitlines()]

    compressed_lines: list[str] = []
    blank_line_pending = False
    for line in lines:
        if line:
            if blank_line_pending and compressed_lines:
                compressed_lines.append("")
            compressed_lines.append(line)
            blank_line_pending = False
        elif compressed_lines:
            blank_line_pending = True

    return "\n".join(compressed_lines)