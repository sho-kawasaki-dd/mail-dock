import os
import time
from pathlib import Path

from mail_dock.infrastructure.logging_config import purge_old_logs


def _set_age(path: Path, *, days: int) -> None:
    timestamp = time.time() - days * 24 * 60 * 60
    os.utime(path, (timestamp, timestamp))


def test_purge_old_logs_removes_only_expired_sync_logs(tmp_path: Path) -> None:
    expired_sync = tmp_path / "sync-2020-01-01.log"
    current_sync = tmp_path / "sync-2026-08-29.log"
    expired_application = tmp_path / "app.log"
    rotated_application = tmp_path / "app.log.1"
    for path in (expired_sync, current_sync, expired_application, rotated_application):
        path.write_text("log", encoding="utf-8")
    _set_age(expired_sync, days=91)
    _set_age(expired_application, days=91)
    _set_age(rotated_application, days=91)

    assert purge_old_logs(tmp_path, days=90) == 1
    assert not expired_sync.exists()
    assert current_sync.exists()
    # Application logs have their own rotation policy and are never purged here.
    assert expired_application.exists()
    assert rotated_application.exists()


def test_purge_old_logs_rejects_negative_retention(tmp_path: Path) -> None:
    try:
        purge_old_logs(tmp_path, days=-1)
    except ValueError as error:
        assert str(error) == "days must be non-negative"
    else:
        raise AssertionError("negative retention must be rejected")
