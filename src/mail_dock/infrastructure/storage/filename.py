"""Compatibility exports for attachment filename safety helpers."""

from mail_dock.domain.attachments import (
    FALLBACK_ATTACHMENT_NAME,
    MAX_FILENAME_BYTES,
    SanitizedName,
    resolve_within,
    sanitize_attachment_name,
)

__all__ = [
    "FALLBACK_ATTACHMENT_NAME",
    "MAX_FILENAME_BYTES",
    "SanitizedName",
    "resolve_within",
    "sanitize_attachment_name",
]
