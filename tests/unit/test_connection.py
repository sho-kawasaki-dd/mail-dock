import threading
from pathlib import Path

import pytest

from mail_dock.domain.errors import DatabaseError, StorageDetachedError
from mail_dock.infrastructure.database.connection import ConnectionManager, connect


def test_readonly_connection_does_not_create_database(tmp_path: Path) -> None:
    db_path = tmp_path / "missing.db"

    with pytest.raises(StorageDetachedError):
        connect(db_path, readonly=True)

    assert not db_path.exists()


def test_readonly_connection_preserves_journal_mode(tmp_path: Path) -> None:
    db_path = tmp_path / "metadata.db"
    writable = connect(db_path)
    writable.execute("CREATE TABLE sample (value TEXT)")
    writable.commit()
    writable.close()

    readonly = connect(db_path, readonly=True, journal_mode="DELETE")
    try:
        assert readonly.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert readonly.execute("PRAGMA query_only").fetchone() == (1,)
    finally:
        readonly.close()


@pytest.mark.parametrize("journal_mode", ["WAL", "DELETE"])
def test_writable_connection_applies_journal_mode(tmp_path: Path, journal_mode: str) -> None:
    connection = connect(tmp_path / "metadata.db", journal_mode=journal_mode)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone() == (journal_mode.lower(),)
    finally:
        connection.close()


def test_invalid_journal_mode_is_rejected_before_pragma(tmp_path: Path) -> None:
    with pytest.raises(DatabaseError, match="journal_mode"):
        connect(tmp_path / "metadata.db", journal_mode="TRUNCATE")


def test_connection_manager_passes_journal_mode(tmp_path: Path) -> None:
    manager = ConnectionManager(tmp_path / "metadata.db", journal_mode="DELETE")
    connection = manager.get_connection()
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    finally:
        manager.close_current_thread()


def test_connection_manager_closes_connections_on_their_own_threads(tmp_path: Path) -> None:
    manager = ConnectionManager(tmp_path / "metadata.db")
    connection_ids: list[int] = []
    worker_errors: list[BaseException] = []

    def worker() -> None:
        try:
            connection_ids.append(id(manager.get_connection()))
            manager.close_current_thread()
        except BaseException as error:
            worker_errors.append(error)

    thread = threading.Thread(target=worker, name="connection-worker")
    thread.start()
    thread.join()

    manager.request_close_all()
    manager.assert_all_closed()

    assert not worker_errors
    assert len(connection_ids) == 1
    with pytest.raises(DatabaseError):
        manager.get_connection()


def test_connection_manager_does_not_share_thread_connections(tmp_path: Path) -> None:
    manager = ConnectionManager(tmp_path / "metadata.db")
    connection_ids: list[int] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        connection_ids.append(id(manager.get_connection()))
        barrier.wait()
        manager.close_current_thread()

    first = threading.Thread(target=worker, name="first-worker")
    second = threading.Thread(target=worker, name="second-worker")
    first.start()
    second.start()
    first.join()
    second.join()

    manager.request_close_all()
    manager.assert_all_closed()

    assert len(connection_ids) == 2
    assert connection_ids[0] != connection_ids[1]
