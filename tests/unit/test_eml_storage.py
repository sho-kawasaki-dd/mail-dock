import ctypes
import hashlib
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO, cast

import pytest

from mail_dock.domain.errors import StorageDetachedError, StorageError
from mail_dock.infrastructure.storage.eml_storage import (
    EmlStorage,
    cleanup_tmp,
    save_eml,
    validate_account_id,
)


def test_save_eml_uses_hash_and_internal_date(tmp_storage_root: Path) -> None:
    raw = b"Subject: hello\n\nbody\n"

    stored = save_eml(
        tmp_storage_root,
        "account@example.com",
        datetime(2026, 7, 30, 12, tzinfo=UTC),
        raw,
    )

    expected_hash = hashlib.sha256(raw).hexdigest()
    assert stored.relative_path == f"eml/account@example.com/2026/07/{expected_hash[:32]}.eml"
    assert stored.file_hash == expected_hash
    assert stored.size_bytes == len(raw)
    assert not stored.deduplicated
    assert (tmp_storage_root / stored.relative_path).read_bytes() == raw
    assert list((tmp_storage_root / "tmp").iterdir()) == []


def test_save_eml_uses_unknown_directory_without_internal_date(tmp_storage_root: Path) -> None:
    stored = save_eml(tmp_storage_root, "account", None, b"body")

    assert stored.relative_path.startswith("eml/account/unknown/")


def test_deduplication_validates_complete_hash_without_writing_tmp(
    tmp_storage_root: Path,
) -> None:
    storage = EmlStorage(tmp_storage_root)
    raw = b"same content"
    first = storage.save("account", None, raw)

    second = storage.save("account", datetime(2030, 1, 1, tzinfo=UTC), raw)

    assert second.relative_path == first.relative_path
    assert second.file_hash == first.file_hash
    assert second.size_bytes == first.size_bytes
    assert second.deduplicated
    assert not list((tmp_storage_root / "tmp").iterdir())


def test_same_prefix_with_wrong_full_hash_is_not_reused(tmp_storage_root: Path) -> None:
    raw = b"new"
    file_hash = hashlib.sha256(raw).hexdigest()
    destination = tmp_storage_root / "eml" / "account" / "unknown" / f"{file_hash[:32]}.eml"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"different content")

    stored = save_eml(tmp_storage_root, "account", None, raw)

    assert not stored.deduplicated
    assert destination.read_bytes() == raw


def test_save_cleans_tmp_when_replace_fails(
    tmp_storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "mail_dock.infrastructure.storage.eml_storage.os.replace",
        lambda source, destination: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(OSError, match="replace failed"):
        save_eml(tmp_storage_root, "account", None, b"body")

    assert list((tmp_storage_root / "tmp").iterdir()) == []


def test_cleanup_tmp_leaves_pst_import_staging(tmp_storage_root: Path) -> None:
    (tmp_storage_root / "tmp" / "pstimp").mkdir(parents=True)
    (tmp_storage_root / "tmp" / "pstimp" / "keep").write_bytes(b"pst")
    (tmp_storage_root / "tmp" / "orphan.eml").write_bytes(b"orphan")

    assert cleanup_tmp(tmp_storage_root) == 1
    assert (tmp_storage_root / "tmp" / "pstimp" / "keep").exists()
    assert not (tmp_storage_root / "tmp" / "orphan.eml").exists()


@pytest.mark.parametrize("account_id", ["", ".", "..", "a/b", "a\\b", "CON", "mail."])
def test_validate_account_id_rejects_unsafe_values(account_id: str) -> None:
    with pytest.raises(ValueError):
        validate_account_id(account_id)


def test_read_and_reuse_reject_paths_outside_root(tmp_storage_root: Path) -> None:
    storage = EmlStorage(tmp_storage_root)

    with pytest.raises(ValueError, match="escapes"):
        storage.read("../outside.eml")
    with pytest.raises(ValueError, match="escapes"):
        storage.read_verified("../outside.eml", "0" * 64)
    with pytest.raises(ValueError, match="escapes"):
        storage.reuse("../outside.eml", "0" * 64)


def test_read_verified_returns_bytes_only_for_a_matching_complete_hash(
    tmp_storage_root: Path,
) -> None:
    storage = EmlStorage(tmp_storage_root)
    raw = b"verified EML content"
    stored = storage.save("account", None, raw)

    assert storage.read_verified(stored.relative_path, stored.file_hash) == raw

    with pytest.raises(StorageError, match="hash"):
        storage.read_verified(stored.relative_path, "0" * 64)


def test_integrity_and_purge_storage_ports_are_bounded_and_idempotent(
    tmp_storage_root: Path,
) -> None:
    storage = EmlStorage(tmp_storage_root)
    raw = b"integrity content"
    stored = storage.save("account", None, raw)

    assert storage.stat(stored.relative_path).st_size == len(raw)
    assert b"".join(storage.iter_chunks(stored.relative_path, chunk_size=3)) == raw
    assert list(storage.iter_eml_paths("account")) == [stored.relative_path]
    assert storage.exists(stored.relative_path)

    storage.quarantine(stored.relative_path)
    assert not storage.exists(stored.relative_path)
    storage.delete(stored.relative_path)

    with pytest.raises(ValueError, match="account_id"):
        list(storage.iter_eml_paths("../outside"))


def _open_with_share_delete(path: Path) -> BinaryIO:
    """Open via CreateFileW with FILE_SHARE_DELETE so a concurrent replace can succeed."""
    import msvcrt
    from ctypes import wintypes

    ctypes_windows: Any = ctypes
    msvcrt_windows: Any = msvcrt
    generic_read = 0x80000000
    file_share_read = 0x00000001
    file_share_write = 0x00000002
    file_share_delete = 0x00000004
    open_existing = 3
    file_attribute_normal = 0x80
    invalid_handle_value = wintypes.HANDLE(-1).value

    kernel32 = ctypes_windows.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE

    handle = kernel32.CreateFileW(
        str(path),
        generic_read,
        file_share_read | file_share_write | file_share_delete,
        None,
        open_existing,
        file_attribute_normal,
        None,
    )
    if handle == invalid_handle_value:
        raise ctypes_windows.WinError(ctypes_windows.get_last_error())
    fd = msvcrt_windows.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    return cast(BinaryIO, os.fdopen(fd, "rb"))


def _replace_with_posix_semantics(source: Path, target: Path) -> None:
    """Rename source onto target the way os.replace() cannot: while target has an open reader.

    Plain os.replace()/MoveFileExW refuses to replace a file with any open handle on Windows,
    regardless of share flags, so this uses SetFileInformationByHandle(FileRenameInfoEx) with
    the POSIX-semantics flag (Windows 10 1709+), which only requires the target's existing
    handle to have been opened with FILE_SHARE_DELETE.
    """
    import struct
    from ctypes import wintypes

    ctypes_windows: Any = ctypes
    delete_access = 0x00010000
    file_share_read = 0x00000001
    file_share_write = 0x00000002
    file_share_delete = 0x00000004
    open_existing = 3
    file_attribute_normal = 0x80
    file_rename_info_ex = 22
    replace_if_exists = 0x1
    posix_semantics = 0x2
    invalid_handle_value = wintypes.HANDLE(-1).value

    kernel32 = ctypes_windows.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.CreateFileW(
        str(source),
        delete_access,
        file_share_read | file_share_write | file_share_delete,
        None,
        open_existing,
        file_attribute_normal,
        None,
    )
    if handle == invalid_handle_value:
        raise ctypes_windows.WinError(ctypes_windows.get_last_error())
    try:
        name = str(target).encode("utf-16-le")
        flags = replace_if_exists | posix_semantics
        buffer = struct.pack("<I4xQI", flags, 0, len(name)) + name
        if not kernel32.SetFileInformationByHandle(
            handle, file_rename_info_ex, buffer, len(buffer)
        ):
            raise ctypes_windows.WinError(ctypes_windows.get_last_error())
    finally:
        kernel32.CloseHandle(handle)


def test_read_verified_rejects_path_replacement_during_read(
    tmp_storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = EmlStorage(tmp_storage_root)
    raw = b"original EML content"
    stored = storage.save("account", None, raw)
    target = tmp_storage_root / stored.relative_path
    original_open = cast(Any, Path.open)

    class ReplacingReader:
        def __init__(self, file: BinaryIO) -> None:
            self._file = file
            self._replaced = False

        def __enter__(self) -> object:
            self._file.__enter__()
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            self._file.__exit__(exc_type, exc_value, traceback)

        def fileno(self) -> int:
            return self._file.fileno()

        def read(self, size: int = -1) -> bytes:
            payload = self._file.read(size)
            if not self._replaced:
                replacement = target.with_suffix(".replacement")
                replacement.write_bytes(b"replacement EML content")
                if sys.platform == "win32":
                    _replace_with_posix_semantics(replacement, target)
                else:
                    replacement.replace(target)
                self._replaced = True
            return payload

        def __getattr__(self, name: str) -> object:
            return getattr(self._file, name)

    def open_with_replacement(path: Path, *args: Any, **kwargs: Any) -> BinaryIO | ReplacingReader:
        if path != target:
            return cast(BinaryIO, original_open(path, *args, **kwargs))
        # Default open() lacks FILE_SHARE_DELETE on Windows, which would block the replace below.
        file = (
            _open_with_share_delete(path)
            if sys.platform == "win32"
            else original_open(path, *args, **kwargs)
        )
        return ReplacingReader(file)

    monkeypatch.setattr(Path, "open", open_with_replacement)

    with pytest.raises(StorageError, match="changed"):
        storage.read_verified(stored.relative_path, stored.file_hash)


def test_detached_storage_error_is_classified(
    tmp_storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "mail_dock.infrastructure.storage.eml_storage.os.replace",
        lambda source, destination: (_ for _ in ()).throw(OSError(5, "device detached")),
    )

    with pytest.raises(StorageDetachedError):
        save_eml(tmp_storage_root, "account", None, b"body")
