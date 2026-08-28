"""Logging setup and privacy filters for mail-dock.

Callers must never pass message bodies to the logger. Subjects are limited to
their first 20 characters, email addresses are masked, and values associated
with password, token, or secret keys are replaced before a record is emitted.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import date
from logging.handlers import RotatingFileHandler
from pathlib import Path

from mail_dock.domain.masking import mask_emails, mask_subject

_APP_LOG_MAX_BYTES = 5 * 1024 * 1024
_APP_LOG_BACKUP_COUNT = 5
_SENSITIVE_VALUE_RE = re.compile(r"(?i)(\b(?:password|token|secret)[\w.-]*\s*[:=]\s*)[^\s,;}\]]+")
_SENSITIVE_KEY_PARTS = ("password", "token", "secret")
_FORMATTER = logging.Formatter(
    "%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)

_app_handler: RotatingFileHandler | None = None
_storage_handler: logging.FileHandler | None = None
_console_handler: logging.Handler | None = None

__all__ = ["MaskingFilter", "mask_subject", "purge_old_logs", "setup_logging"]


def _mask_text(value: str) -> str:
    masked = _SENSITIVE_VALUE_RE.sub(r"\1***", value)
    return mask_emails(masked)


class MaskingFilter(logging.Filter):
    """Mask personal and credential data in log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = _mask_text(record.getMessage())
        record.msg = message
        record.args = ()

        for key in tuple(record.__dict__):
            if isinstance(key, str) and any(part in key.lower() for part in _SENSITIVE_KEY_PARTS):
                record.__dict__[key] = "***"
        return True


def _new_file_handler(path: Path) -> logging.FileHandler:
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(_FORMATTER)
    handler.addFilter(MaskingFilter())
    return handler


def set_application_log_target(config_dir: Path) -> None:
    """Attach the application log to the local configuration directory."""
    global _app_handler

    logger = _logger()
    log_dir = config_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    _remove_handler(logger, _app_handler)
    _app_handler = RotatingFileHandler(
        log_dir / "app.log",
        maxBytes=_APP_LOG_MAX_BYTES,
        backupCount=_APP_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    _app_handler.setFormatter(_FORMATTER)
    _app_handler.addFilter(MaskingFilter())
    logger.addHandler(_app_handler)


def _logger() -> logging.Logger:
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger


def _remove_handler(logger: logging.Logger, handler: logging.Handler | None) -> None:
    if handler is None:
        return
    logger.removeHandler(handler)
    handler.close()


def setup_logging(config_dir: Path, *, debug: bool) -> None:
    """Configure mandatory application-file and optional console logging."""
    global _console_handler

    logger = _logger()
    set_application_log_target(config_dir)

    _remove_handler(logger, _console_handler)
    _console_handler = None

    if debug or "MAILDOCK_DEBUG" in os.environ:
        _console_handler = logging.StreamHandler()
        _console_handler.setFormatter(_FORMATTER)
        _console_handler.addFilter(MaskingFilter())
        logger.addHandler(_console_handler)


def set_storage_log_target(path: Path | None) -> None:
    """Attach or detach the current day's storage synchronization log."""
    global _storage_handler

    logger = _logger()
    _remove_handler(logger, _storage_handler)
    _storage_handler = None
    if path is None:
        return

    path.mkdir(parents=True, exist_ok=True)
    _storage_handler = _new_file_handler(path / f"sync-{date.today().isoformat()}.log")
    logger.addHandler(_storage_handler)


def purge_old_logs(log_dir: Path, days: int = 90) -> int:
    """Delete log files older than ``days`` and return the number removed."""
    if days < 0:
        raise ValueError("days must be non-negative")

    cutoff = time.time() - days * 24 * 60 * 60
    removed = 0
    for path in log_dir.glob("*.log*"):
        if not path.is_file() or path.stat().st_mtime >= cutoff:
            continue
        path.unlink()
        removed += 1
    return removed
