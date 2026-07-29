import errno
import sqlite3

import pytest

from mail_dock.domain.errors import StorageDetachedError
from mail_dock.infrastructure.storage.detach import (
    classify_os_error,
    classify_sqlite_error,
    storage_io,
)


class NamedSQLiteError(sqlite3.Error):
    sqlite_errorname: str

    def __init__(self, error_name: str) -> None:
        super().__init__(error_name)
        self.sqlite_errorname = error_name


class WindowsOSError(OSError):
    winerror: int

    def __init__(self, winerror: int) -> None:
        super().__init__("detached")
        self.winerror = winerror


@pytest.mark.parametrize("winerror", [6, 21, 55, 433, 995, 1117, 1167])
def test_windows_detach_errors_are_classified(winerror: int) -> None:
    classified = classify_os_error(WindowsOSError(winerror))

    assert isinstance(classified, StorageDetachedError)


@pytest.mark.parametrize("error_number", [errno.EIO, errno.ENXIO, errno.ENODEV, errno.ESTALE])
def test_posix_detach_errors_are_classified(error_number: int) -> None:
    classified = classify_os_error(OSError(error_number, "detached"))

    assert isinstance(classified, StorageDetachedError)


def test_unrelated_os_error_is_returned_unchanged() -> None:
    error = OSError(errno.EPERM, "permission denied")

    assert classify_os_error(error) is error


@pytest.mark.parametrize(
    "error_name",
    ["SQLITE_IOERR_READ", "SQLITE_READONLY_DBMOVED", "SQLITE_CANTOPEN"],
)
def test_sqlite_detach_errors_are_classified(error_name: str) -> None:
    classified = classify_sqlite_error(NamedSQLiteError(error_name))

    assert isinstance(classified, StorageDetachedError)


def test_storage_io_reraises_classified_errors() -> None:
    with pytest.raises(StorageDetachedError), storage_io():
        raise OSError(errno.EIO, "detached")
