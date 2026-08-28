"""Pure text-masking helpers shared by logging and read-only audit displays.

Kept dependency-free (unlike ``infrastructure.logging_config``) so that
presentation code showing historical records (e.g. the audit log view) can
mask subjects and email addresses the same way logs do, without importing
across the presentation/infrastructure boundary.
"""

from __future__ import annotations

import re

_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?P<local>[A-Za-z0-9.!#$%&'*+/?^_`{|}~-]+)@(?P<domain>"
    r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,})"
)


def _mask_email(match: re.Match[str]) -> str:
    local = match.group("local")
    masked_local = f"{local[:2]}***" if len(local) > 1 else f"{local}***"
    return f"{masked_local}@{match.group('domain')}"


def mask_emails(text: str) -> str:
    """Mask the local part of any email address found in ``text``."""

    return _EMAIL_RE.sub(_mask_email, text)


def mask_subject(subject: str) -> str:
    """Return a subject limited to 20 characters, with an omission marker."""

    if len(subject) <= 20:
        return subject
    return f"{subject[:20]}..."
