"""Parse EML bytes into provider-independent message metadata."""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from datetime import datetime
from email import policy
from email.message import Message
from email.parser import BytesParser

from mail_dock.domain.messages import ParsedAttachment, ParsedMessage

from .charset import decode_text
from .headers import (
    decode_header_value,
    derive_content_key,
    derive_thread_key,
    parse_content_disposition_filename,
    parse_date_header,
)
from .html_to_text import html_to_text

_WHITESPACE_RE = re.compile(r"\s+")


def _header_value(message: Message, name: str) -> str:
    value = message.get(name)
    return decode_header_value(str(value)) if value is not None else ""


def _optional_header_value(message: Message, name: str) -> str | None:
    value = _header_value(message, name)
    return value or None


def _message_defects(message: Message) -> list[str]:
    defects: list[str] = []
    for part in message.walk():
        defects.extend(type(defect).__name__ for defect in part.defects)
    return defects


def _validate_base64_payloads(message: Message) -> None:
    for part in message.walk():
        if part.is_multipart() or part.get("Content-Transfer-Encoding", "").lower() != "base64":
            continue
        payload = part.get_payload(decode=False)
        if not isinstance(payload, str):
            continue
        encoded = _WHITESPACE_RE.sub("", payload)
        try:
            base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("invalid base64 payload") from error


def _decoded_payload(part: Message) -> bytes:
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        return payload.encode("utf-8", errors="replace")
    return b""


def _attachment_for_part(part: Message) -> ParsedAttachment | None:
    content_type = part.get_content_type()
    disposition = part.get_content_disposition()
    filename = parse_content_disposition_filename(part)
    content_id = part.get("Content-ID")
    is_inline = bool(content_id) and disposition != "attachment"
    is_attachment = (
        disposition in {"attachment", "inline"}
        or filename is not None
        or is_inline
        or part.get_content_maintype() not in {"text", "message"}
    )
    if not is_attachment:
        return None
    return ParsedAttachment(
        filename=filename,
        content_type=content_type,
        size_bytes=len(_decoded_payload(part)),
        is_inline=is_inline,
    )


def _body_candidate(part: Message) -> tuple[str, str] | None:
    if part.is_multipart() or part.get_content_maintype() != "text":
        return None
    if part.get_content_disposition() == "attachment":
        return None
    if parse_content_disposition_filename(part) is not None:
        return None

    text, _ = decode_text(_decoded_payload(part), part.get_content_charset())
    if part.get_content_subtype().lower() == "html":
        return "html", html_to_text(text)
    if part.get_content_subtype().lower() == "plain":
        return "plain", text
    return None


def _extract_content(message: Message) -> tuple[str, tuple[ParsedAttachment, ...]]:
    plain_candidates: list[str] = []
    html_candidates: list[str] = []
    attachments: list[ParsedAttachment] = []

    for part in message.walk():
        if part.is_multipart():
            continue
        attachment = _attachment_for_part(part)
        if attachment is not None:
            attachments.append(attachment)
        candidate = _body_candidate(part)
        if candidate is None:
            continue
        content_type, text = candidate
        if content_type == "plain":
            plain_candidates.append(text)
        else:
            html_candidates.append(text)

    body_text = next((text for text in plain_candidates if text.strip()), "")
    if not body_text:
        body_text = next((text for text in html_candidates if text.strip()), "")
    return body_text, tuple(attachments)


def _format_parse_error(defect_names: list[str], error: Exception | None) -> str | None:
    details = list(dict.fromkeys(defect_names))
    if error is not None:
        details.append(type(error).__name__)
    return "; ".join(details) or None


def parse_eml(raw: bytes, internal_date: datetime | None) -> ParsedMessage:
    """Parse an EML message without leaking parsing exceptions to callers."""
    message_id: str | None = None
    in_reply_to: str | None = None
    references_ids: str | None = None
    subject = ""
    sender = ""
    recipient = ""
    cc = ""
    date_sent: datetime | None = None
    body_text = ""
    attachments: tuple[ParsedAttachment, ...] = ()
    defect_names: list[str] = []
    parse_exception: Exception | None = None
    eml_sha256 = hashlib.sha256(raw).hexdigest()

    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
        defect_names = _message_defects(message)
        subject = _header_value(message, "Subject")
        sender = _header_value(message, "From")
        recipient = _header_value(message, "To")
        cc = _header_value(message, "Cc")
        message_id = _optional_header_value(message, "Message-ID")
        in_reply_to = _optional_header_value(message, "In-Reply-To")
        references_ids = _optional_header_value(message, "References")
        date_sent = parse_date_header(message.get("Date"), internal_date)
        _validate_base64_payloads(message)
        body_text, attachments = _extract_content(message)
    except Exception as error:
        parse_exception = error

    return ParsedMessage(
        subject=subject,
        sender=sender,
        recipient=recipient,
        cc=cc,
        date_sent=date_sent,
        message_id=message_id,
        in_reply_to=in_reply_to,
        references_ids=references_ids,
        thread_key=derive_thread_key(message_id, in_reply_to, references_ids),
        content_key=derive_content_key(message_id, eml_sha256),
        body_text=body_text,
        attachments=attachments,
        has_attachment=any(not attachment.is_inline for attachment in attachments),
        parse_error=_format_parse_error(defect_names, parse_exception),
    )