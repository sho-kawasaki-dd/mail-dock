import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mail_dock.domain.errors import StorageLockedError
from mail_dock.infrastructure.storage.storage_root import StorageLock


def test_second_lock_acquisition_fails(tmp_storage_root: Path) -> None:
    first = StorageLock(tmp_storage_root, retry_attempts=1, retry_delay_sec=0).acquire()
    second = StorageLock(tmp_storage_root, retry_attempts=1, retry_delay_sec=0)
    try:
        with pytest.raises(StorageLockedError):
            second.acquire()
    finally:
        first.release()


def test_stale_heartbeat_is_recovered(tmp_storage_root: Path) -> None:
    lock_path = tmp_storage_root / ".lock"
    metadata_path = tmp_storage_root / ".lock.meta.json"
    lock_path.touch()
    metadata_path.write_text(
        json.dumps(
            {
                "pid": 1,
                "instance_uuid": "old",
                "machine_id": "old-machine",
                "heartbeat_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    lock = StorageLock(tmp_storage_root, retry_attempts=1, retry_delay_sec=0).acquire()
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        assert metadata["instance_uuid"] != "old"
    finally:
        lock.release()


def test_release_removes_lock_files(tmp_storage_root: Path) -> None:
    lock = StorageLock(tmp_storage_root, retry_attempts=1, retry_delay_sec=0).acquire()

    lock.release()

    assert not (tmp_storage_root / ".lock").exists()
    assert not (tmp_storage_root / ".lock.meta.json").exists()


def test_existing_unlocked_lock_file_does_not_block_startup(tmp_storage_root: Path) -> None:
    (tmp_storage_root / ".lock").touch()
    lock = StorageLock(tmp_storage_root, retry_attempts=1, retry_delay_sec=0).acquire()

    lock.release()

    assert not (tmp_storage_root / ".lock").exists()
