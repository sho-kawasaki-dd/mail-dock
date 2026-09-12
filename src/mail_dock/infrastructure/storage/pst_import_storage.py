"""Filesystem implementation of the PST import storage port."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import replace
from email import policy
from email.parser import BytesHeaderParser
from pathlib import Path

from mail_dock.domain.errors import SourceChangedError, StorageDetachedError, UnreadableArchive
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.importer import SourceFileSnapshot, StagedMessage
from mail_dock.domain.messages import ParsedMessage
from mail_dock.domain.ports import BasePstImportStorage, JSONValue
from mail_dock.infrastructure.parsing.eml_parser import parse_eml
from mail_dock.infrastructure.parsing.headers import parse_date_header, to_utc_iso8601

_CHUNK_SIZE = 1024 * 1024
_DATE_HEADER_LIMIT = 64 * 1024
_REPARSE_POINT = 0x400
_STAGE_MARKER_NAME = "stageA_done.json"


def _canonical_json(payload: Mapping[str, JSONValue]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False
    ).encode("utf-8")


def _is_reparse_point(metadata: os.stat_result) -> bool:
    return bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT)


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source_file:
            before = os.fstat(source_file.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise UnreadableArchive(f"Staging item is not a regular file: {path}")
            size = 0
            for chunk in iter(lambda: source_file.read(_CHUNK_SIZE), b""):
                digest.update(chunk)
                size += len(chunk)
            after = os.fstat(source_file.fileno())
    except UnreadableArchive:
        raise
    except OSError as error:
        raise UnreadableArchive(f"Could not inspect staging item: {path}") from error
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise UnreadableArchive(f"Staging item changed while it was scanned: {path}")
    return size, digest.hexdigest()


def _file_identity(metadata: os.stat_result) -> tuple[int, int] | None:
    device = getattr(metadata, "st_dev", None)
    inode = getattr(metadata, "st_ino", None)
    if not isinstance(device, int) or not isinstance(inode, int):
        return None
    return device, inode


def _snapshot_metadata(metadata: os.stat_result) -> tuple[int, int, tuple[int, int] | None]:
    return metadata.st_size, metadata.st_mtime_ns, _file_identity(metadata)


class PstImportStorage(BasePstImportStorage):
    """Local filesystem and parser adapter used by PST import workflows."""

    def normalize_path(self, path: os.PathLike[str]) -> Path:
        return Path(path).expanduser().resolve()

    def source_filename(self, source: os.PathLike[str]) -> str:
        return Path(source).name

    def staging_root(self, storage_root: os.PathLike[str], import_uuid: str) -> Path:
        return (
            self.normalize_path(storage_root) / "tmp" / "pstimp" / import_uuid.replace("-", "")[:8]
        )

    def marker_path(self, staging_root: os.PathLike[str]) -> Path:
        return Path(staging_root) / _STAGE_MARKER_NAME

    def create_staging(self, staging_root: os.PathLike[str]) -> None:
        try:
            Path(staging_root).mkdir(parents=True, exist_ok=False)
        except OSError as error:
            raise StorageDetachedError(f"Could not create PST staging: {staging_root}") from error

    def remove_staging(self, staging_root: os.PathLike[str]) -> None:
        try:
            shutil.rmtree(Path(staging_root), ignore_errors=False)
        except FileNotFoundError:
            return
        except OSError as error:
            raise StorageDetachedError(f"Could not discard PST staging: {staging_root}") from error

    def read_marker(self, marker_path: os.PathLike[str]) -> Mapping[str, JSONValue]:
        try:
            with Path(marker_path).open("rb") as marker_file:
                payload = json.load(marker_file)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UnreadableArchive(f"Stage A marker cannot be read: {marker_path}") from error
        if not isinstance(payload, dict):
            raise UnreadableArchive("Stage A marker must contain a JSON object")
        return payload

    def write_marker(self, marker_path: os.PathLike[str], payload: Mapping[str, JSONValue]) -> None:
        marker = Path(marker_path)
        encoded = _canonical_json(payload) + b"\n"
        temporary_path = marker.with_name(f".{marker.name}.{os.urandom(16).hex()}.tmp")
        try:
            with temporary_path.open("wb") as marker_file:
                marker_file.write(encoded)
                marker_file.flush()
                os.fsync(marker_file.fileno())
            temporary_path.replace(marker)
            if os.name != "nt":
                directory_fd = os.open(marker.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except OSError as error:
            raise StorageDetachedError(f"Could not persist Stage A marker: {marker}") from error
        finally:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)

    def scan_staging(
        self, staging_root: os.PathLike[str]
    ) -> tuple[list[dict[str, JSONValue]], list[dict[str, JSONValue]]]:
        try:
            root = Path(staging_root).resolve(strict=True)
        except OSError as error:
            raise UnreadableArchive(
                f"Staging directory cannot be inspected: {staging_root}"
            ) from error
        if not root.is_dir():
            raise UnreadableArchive(f"Staging path is not a directory: {staging_root}")

        items: list[dict[str, JSONValue]] = []
        folders: set[str] = set()
        pending = [root]
        while pending:
            current = pending.pop()
            try:
                entries = sorted(os.scandir(current), key=lambda entry: entry.name)
            except OSError as error:
                raise UnreadableArchive(
                    f"Could not inspect staging directory: {current}"
                ) from error
            for entry in entries:
                candidate = Path(entry.path)
                if candidate == root / _STAGE_MARKER_NAME:
                    continue
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if entry.is_symlink() or _is_reparse_point(metadata):
                    continue
                try:
                    resolved = candidate.resolve(strict=False)
                    resolved.relative_to(root)
                except (OSError, ValueError):
                    continue
                if stat.S_ISDIR(metadata.st_mode):
                    pending.append(candidate)
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    continue
                relative_path = candidate.relative_to(root).as_posix()
                folder_relative_path = candidate.parent.relative_to(root).as_posix() or "."
                size_bytes, file_hash = _hash_file(candidate)
                items.append(
                    {
                        "source_item_key": relative_path,
                        "source_relative_path": relative_path,
                        "folder_relative_path": folder_relative_path,
                        "source_size_bytes": size_bytes,
                        "source_sha256": file_hash,
                    }
                )
                folder_path = candidate.parent
                while folder_path != root:
                    folders.add(folder_path.relative_to(root).as_posix())
                    folder_path = folder_path.parent

        items.sort(key=lambda item: str(item["source_item_key"]))
        folder_records: list[dict[str, JSONValue]] = [
            {"raw_name": folder_name, "display_name": folder_name.rsplit("/", 1)[-1]}
            for folder_name in sorted(folders)
        ]
        return items, folder_records

    def resolve_staging_item(self, staging_root: os.PathLike[str], relative_path: str) -> Path:
        root = Path(staging_root).resolve()
        candidate = (root / Path(relative_path)).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise UnreadableArchive(
                f"PST staging item escapes its root: {relative_path}"
            ) from error
        if not candidate.is_file():
            raise UnreadableArchive(f"PST staging item is missing: {relative_path}")
        return candidate

    def read_staged_message(self, source_path: os.PathLike[str], *, parse: bool) -> StagedMessage:
        if not parse:
            return StagedMessage(ParsedMessage())
        path = Path(source_path)
        try:
            parsed = parse_eml(path.read_bytes(), None)
            with path.open("rb") as source_file:
                prefix = source_file.read(_DATE_HEADER_LIMIT)
        except OSError as error:
            raise UnreadableArchive(f"Could not read PST staging item: {path}") from error
        if b"\n\n" in prefix or b"\r\n\r\n" in prefix:
            header = BytesHeaderParser(policy=policy.default).parsebytes(prefix)
            parsed = replace(parsed, date_sent=parse_date_header(header.get("Date"), None))
        return StagedMessage(
            parsed,
            to_utc_iso8601(parsed.date_sent) if parsed.date_sent is not None else None,
        )

    def snapshot_source_file(
        self,
        source: os.PathLike[str],
        *,
        cancel: CancelToken | None = None,
        chunk_size: int = _CHUNK_SIZE,
    ) -> SourceFileSnapshot:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        token = cancel or CancelToken()
        token.raise_if_cancelled()
        digest = hashlib.sha256()
        source_path = Path(source)
        try:
            with source_path.open("rb") as source_file:
                initial = os.fstat(source_file.fileno())
                if not stat.S_ISREG(initial.st_mode):
                    raise UnreadableArchive(f"PST source is not a regular file: {source_path}")
                for chunk in iter(lambda: source_file.read(chunk_size), b""):
                    token.raise_if_cancelled()
                    digest.update(chunk)
                final = os.fstat(source_file.fileno())
        except UnreadableArchive:
            raise
        except OSError as error:
            raise UnreadableArchive(f"Could not read PST source: {source_path}") from error

        if _snapshot_metadata(initial) != _snapshot_metadata(final):
            raise SourceChangedError(f"PST source changed while it was being read: {source_path}")
        return SourceFileSnapshot(
            source_sha256=digest.hexdigest(),
            size_bytes=final.st_size,
            mtime_ns=final.st_mtime_ns,
            file_identity=_file_identity(final),
        )

    def relative_path(self, root: os.PathLike[str], child: os.PathLike[str]) -> str:
        return Path(child).relative_to(Path(root)).as_posix()
