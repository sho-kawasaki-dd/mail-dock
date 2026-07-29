import logging
from pathlib import Path

from mail_dock.infrastructure.logging_config import (
    mask_subject,
    set_storage_log_target,
    setup_logging,
)


def test_logging_masks_email_and_sensitive_values(tmp_path: Path) -> None:
    setup_logging(tmp_path, debug=False)
    logger = logging.getLogger("mail-dock.test")
    logger.info("from user@example.com password=supersecret token=abc123")

    for handler in logging.getLogger().handlers:
        handler.flush()
    output = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")

    assert "us***@example.com" in output
    assert "supersecret" not in output
    assert "abc123" not in output
    assert "password=***" in output
    assert "token=***" in output


def test_mask_subject_truncates_after_twenty_characters() -> None:
    assert mask_subject("short subject") == "short subject"
    assert mask_subject("123456789012345678901") == "12345678901234567890..."


def test_set_storage_log_target_none_removes_storage_handler(tmp_path: Path) -> None:
    setup_logging(tmp_path / "config", debug=False)
    storage_dir = tmp_path / "storage" / "logs"
    set_storage_log_target(storage_dir)

    assert any(
        isinstance(handler, logging.FileHandler)
        and Path(handler.baseFilename).parent == storage_dir
        for handler in logging.getLogger().handlers
    )

    set_storage_log_target(None)

    assert not any(
        isinstance(handler, logging.FileHandler)
        and Path(handler.baseFilename).parent == storage_dir
        for handler in logging.getLogger().handlers
    )
