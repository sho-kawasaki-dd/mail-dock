from __future__ import annotations

from pathlib import Path

import pytest

from mail_dock.domain.messages import RenderedMessage
from mail_dock.infrastructure.parsing.eml_render import (
    EmlMessageRenderer,
    extract_render_parts,
)

FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "eml"


def _render_fixture(name: str) -> RenderedMessage:
    return extract_render_parts((FIXTURE_DIR / name).read_bytes())


def test_renderer_extracts_bodies_and_preserves_mime_walk_order() -> None:
    rendered = _render_fixture("16_multipart_related_inline_image.eml")

    assert rendered.text_body == ""
    assert rendered.html_body == (
        '<html><body><p>Inline image</p><img src="cid:image-1"></body></html>'
    )
    assert len(rendered.parts) == 1
    assert rendered.parts[0].content_id == "image-1"
    assert rendered.parts[0].filename == "logo.png"
    assert rendered.parts[0].is_inline is True


def test_renderer_keeps_regular_attachment_and_inline_part_indices_stable() -> None:
    rendered = _render_fixture("17_nested_multipart.eml")

    assert rendered.text_body == "Nested plain body."
    assert rendered.html_body == "<p>Nested HTML body.</p>"
    assert [part.filename for part in rendered.parts] == ["nested.txt"]
    assert rendered.parts[0].is_inline is False


def test_renderer_decodes_declared_japanese_charset() -> None:
    rendered = _render_fixture("02_cp932_machine_chars.eml")

    assert "機種依存文字" in rendered.text_body


@pytest.mark.parametrize("charset", ["iso-2022-jp", "cp932"])
def test_renderer_decodes_html_in_declared_japanese_charsets(charset: str) -> None:
    body = "<p>日本語の本文</p>"
    raw = (
        f"Content-Type: text/html; charset={charset}\r\nContent-Transfer-Encoding: 8bit\r\n\r\n"
    ).encode("ascii") + body.encode(charset)

    rendered = extract_render_parts(raw)

    assert rendered.html_body == body


def test_renderer_recovers_halfwidth_kana_mislabeled_as_plain_iso_2022_jp() -> None:
    # ESC ( I half-width kana is common in real mail but rejected by the strict codec.
    body = b"Contact: \x1b(I\x31\x32\x33\x1b(B desu"
    raw = (
        b"Content-Type: text/plain; charset=iso-2022-jp\r\nContent-Transfer-Encoding: 8bit\r\n\r\n"
    ) + body

    rendered = extract_render_parts(raw)

    assert rendered.text_body == "Contact: ｱｲｳ desu"


def test_renderer_class_implements_renderer_port() -> None:
    renderer = EmlMessageRenderer()

    rendered = renderer.render((FIXTURE_DIR / "18_attachment_only.eml").read_bytes())

    assert isinstance(rendered, RenderedMessage)
