"""Extract displayable EML bodies and MIME parts.

This module deliberately does not share the implementation of ``parse_eml``.
Rendering keeps decoded body HTML and raw attachment bytes for the GUI, while
the search parser produces normalized text and lightweight attachment metadata.
"""

from __future__ import annotations

from email import policy
from email.message import Message
from email.parser import BytesParser

from mail_dock.domain.messages import MessagePart, RenderedMessage
from mail_dock.domain.ports import BaseMessageRenderer

from .charset import decode_text
from .headers import parse_content_disposition_filename


def _decoded_payload(part: Message) -> bytes:
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        return payload.encode("utf-8", errors="replace")
    return b""


def _content_id(part: Message) -> str | None:
    value = part.get("Content-ID")
    if value is None:
        return None
    content_id = str(value).strip()
    if len(content_id) >= 2 and content_id.startswith("<") and content_id.endswith(">"):
        content_id = content_id[1:-1].strip()
    return content_id or None


def _part_metadata(part: Message) -> tuple[str | None, str | None, bool, bool]:
    disposition = part.get_content_disposition()
    filename = parse_content_disposition_filename(part)
    content_id = _content_id(part)
    is_inline = bool(content_id) and disposition != "attachment"
    is_attachment = (
        disposition in {"attachment", "inline"}
        or filename is not None
        or is_inline
        or part.get_content_maintype() not in {"text", "message"}
    )
    return filename, content_id, is_inline, is_attachment


def extract_render_parts(raw: bytes) -> RenderedMessage:
    """Parse EML bytes into bodies and MIME parts in stable walk order.

    ``parts`` contains every regular attachment and inline part encountered by
    ``Message.walk()``. Its zero-based order is therefore suitable for the
    attachment-save use case and remains stable for the same EML input.
    """
    message = BytesParser(policy=policy.default).parsebytes(raw)
    html_body: str | None = None
    text_body = ""
    parts: list[MessagePart] = []

    for part in message.walk():
        if part.is_multipart():
            continue

        payload = _decoded_payload(part)
        filename, content_id, is_inline, is_attachment = _part_metadata(part)
        if is_attachment:
            parts.append(
                MessagePart(
                    content_id=content_id,
                    content_type=part.get_content_type(),
                    filename=filename,
                    payload=payload,
                    is_inline=is_inline,
                )
            )

        if part.get_content_disposition() == "attachment" or filename is not None:
            continue
        if part.get_content_maintype() != "text":
            continue

        text, _ = decode_text(payload, part.get_content_charset())
        subtype = part.get_content_subtype().lower()
        if subtype == "plain" and not text_body.strip():
            text_body = text
        elif subtype == "html" and (html_body is None or not html_body.strip()):
            html_body = text

    return RenderedMessage(html_body=html_body, text_body=text_body, parts=tuple(parts))


class EmlMessageRenderer(BaseMessageRenderer):
    """Render EML bytes using the infrastructure MIME parser."""

    def render(self, raw: bytes) -> RenderedMessage:
        return extract_render_parts(raw)
