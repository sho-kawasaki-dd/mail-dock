"""Helpers for building valid and intentionally unusual EML test messages."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path
from typing import Any, NamedTuple, cast


class AttachmentSpec(NamedTuple):
    filename: str
    content: bytes
    content_type: str = "application/octet-stream"
    disposition: str = "attachment"


def build_eml(
    *,
    subject: str = "Fixture message",
    sender: str = "sender@example.test",
    recipient: str = "recipient@example.test",
    body: str = "Fixture body",
    charset: str = "utf-8",
    message_id: str | None = "<fixture@example.test>",
    date: str | None = "Thu, 30 Jul 2026 12:00:00 +0000",
    headers: Mapping[str, str] | None = None,
    attachments: Iterable[AttachmentSpec] = (),
) -> bytes:
    """Build one EML message with predictable headers and transfer encoding."""

    message = EmailMessage(policy=SMTP)
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    if message_id is not None:
        message["Message-ID"] = message_id
    if date is not None:
        message["Date"] = date
    for name, value in (headers or {}).items():
        message[name] = value
    message.set_content(body, charset=charset)
    for attachment in attachments:
        maintype, subtype = attachment.content_type.split("/", 1)
        message.add_attachment(
            attachment.content,
            maintype=maintype,
            subtype=subtype,
            filename=attachment.filename,
            disposition=attachment.disposition,
        )
    return message.as_bytes()


def build_alternative_email(*, plain: str, html: str, **kwargs: object) -> bytes:
    """Build a multipart/alternative message where plain text is available."""

    message = EmailMessage(policy=SMTP)
    common = {
        "Subject": "Alternative fixture",
        "From": "sender@example.test",
        "To": "recipient@example.test",
    }
    for name, value in common.items():
        message[name] = str(kwargs.pop(name.lower().replace("-", "_"), value))
    message.set_content(plain)
    message.add_alternative(html, subtype="html")
    return message.as_bytes()


def build_related_email(
    *, body_html: str, image: bytes = b"PNG fixture", cid: str = "image-1"
) -> bytes:
    """Build a related HTML body with an inline Content-ID image."""

    message = EmailMessage(policy=SMTP)
    message["Subject"] = "Related fixture"
    message["From"] = "sender@example.test"
    message["To"] = "recipient@example.test"
    message.set_content("Related fallback")
    message.add_alternative(body_html, subtype="html")
    html_part = cast(Any, message.get_payload())[-1]
    html_part.add_related(image, maintype="image", subtype="png", cid=f"<{cid}>")
    return message.as_bytes()


def build_attachment_only(*, attachment: AttachmentSpec) -> bytes:
    """Build a message with no text body and one regular attachment."""

    message = EmailMessage(policy=SMTP)
    message["Subject"] = "Attachment-only fixture"
    message["From"] = "sender@example.test"
    message["To"] = "recipient@example.test"
    message.add_attachment(
        attachment.content,
        maintype=attachment.content_type.split("/", 1)[0],
        subtype=attachment.content_type.split("/", 1)[1],
        filename=attachment.filename,
    )
    return message.as_bytes()


def build_corpus() -> dict[str, bytes]:
    """Return the generated corpus used by tests that need fresh temporary EML."""

    return {
        "utf8.eml": build_eml(body="UTF-8 の本文です。"),
        "multipart_alternative.eml": build_alternative_email(
            plain="Plain text wins.", html="<p>HTML fallback</p>"
        ),
        "multipart_related.eml": build_related_email(
            body_html='<p>Inline image</p><img src="cid:image-1">'
        ),
        "attachment_only.eml": build_attachment_only(
            attachment=AttachmentSpec("report.txt", b"report")
        ),
        "message_id_missing.eml": build_eml(message_id=None),
        "message_id_duplicate.eml": build_eml(message_id="<duplicate@example.test>"),
        "date_invalid.eml": build_eml(date="not-a-date"),
        "date_missing.eml": build_eml(date=None),
        "date_future.eml": build_eml(date="Thu, 30 Jul 2036 12:00:00 +0000"),
    }


def write_corpus(directory: Path, corpus: Mapping[str, bytes] | None = None) -> list[Path]:
    """Write a generated corpus and return paths in deterministic filename order."""

    directory.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for name, raw in sorted((corpus or build_corpus()).items()):
        path = directory / name
        path.write_bytes(raw)
        paths.append(path)
    return paths


make_eml = build_eml