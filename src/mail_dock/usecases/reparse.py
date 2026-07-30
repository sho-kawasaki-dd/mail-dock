"""Reparse already stored EML files without repairing storage integrity."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from mail_dock.domain.errors import OperationCancelledError
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.messages import ParsedMessage
from mail_dock.domain.ports import BaseEmlStorage
from mail_dock.domain.repository import BaseMessageRepository, MessageContents
from mail_dock.infrastructure.parsing.eml_parser import parse_eml

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReparseResult:
    """Summary of a reparse run and records skipped for integrity reasons."""

    reparsed_count: int
    skipped_count: int
    missing_count: int
    hash_mismatch_count: int
    parse_failed_count: int
    cancelled: bool


def _message_contents(parsed: ParsedMessage) -> MessageContents:
    attachment_names = "\n".join(
        attachment.filename
        for attachment in parsed.attachments
        if attachment.filename is not None and not attachment.is_inline
    )
    return {
        "subject_norm": parsed.subject,
        "sender_norm": parsed.sender,
        "body_text": parsed.body_text,
        "attachment_names": attachment_names,
    }


def _internal_date(record: Mapping[str, Any]) -> datetime | None:
    value = record.get("internal_date")
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _clear_parse_failure(repo: BaseMessageRepository, record: Mapping[str, Any]) -> None:
    account_id = record.get("account_id")
    folder_id = record.get("folder_id")
    uidvalidity = record.get("uidvalidity")
    uid = record.get("uid")
    if (
        isinstance(account_id, str)
        and folder_id is not None
        and isinstance(uidvalidity, int)
        and isinstance(uid, int)
    ):
        repo.clear_failure(account_id, folder_id, uidvalidity, uid)


def reparse_messages(
    repo: BaseMessageRepository,
    storage: BaseEmlStorage,
    *,
    account_id: str | None = None,
    only_failed: bool = True,
    cancel: CancelToken | None = None,
) -> ReparseResult:
    """Rebuild searchable contents from integrity-checked stored EML files.

    Missing files and complete-hash mismatches are reported and left untouched.
    The use case deliberately does not repair either condition; that belongs to
    the Phase 4 integrity-check workflow. Records without a stored path, such
    as oversize messages, are excluded by the repository port.
    """

    token = cancel or CancelToken()
    reparsed_count = 0
    skipped_count = 0
    missing_count = 0
    hash_mismatch_count = 0
    parse_failed_count = 0
    cancelled = False

    for record in repo.list_reparse_targets(account_id, only_failed):
        try:
            token.raise_if_cancelled()
        except OperationCancelledError:
            cancelled = True
            break

        relative_path = record.get("relative_path")
        expected_hash = record.get("file_hash")
        message_id = record.get("id")
        if (
            not isinstance(relative_path, str)
            or not isinstance(expected_hash, str)
            or message_id is None
        ):
            skipped_count += 1
            _LOGGER.warning("Skipping invalid reparse target metadata")
            continue

        try:
            raw = storage.read(relative_path)
        except FileNotFoundError:
            missing_count += 1
            skipped_count += 1
            _LOGGER.warning("Stored EML is missing: relative_path=%s", relative_path)
            continue

        actual_hash = hashlib.sha256(raw).hexdigest()
        if actual_hash != expected_hash.casefold():
            hash_mismatch_count += 1
            skipped_count += 1
            _LOGGER.warning("Stored EML hash mismatch: relative_path=%s", relative_path)
            continue

        parsed = parse_eml(raw, _internal_date(record))
        if parsed.parse_error is not None:
            parse_failed_count += 1
            skipped_count += 1
            _LOGGER.warning(
                "Reparse failed: message_id=%s error=%s", message_id, parsed.parse_error
            )
            continue

        repo.begin_batch()
        repo.update_message_contents(message_id, _message_contents(parsed))
        _clear_parse_failure(repo, record)
        repo.commit_batch()
        reparsed_count += 1

    return ReparseResult(
        reparsed_count=reparsed_count,
        skipped_count=skipped_count,
        missing_count=missing_count,
        hash_mismatch_count=hash_mismatch_count,
        parse_failed_count=parse_failed_count,
        cancelled=cancelled,
    )
