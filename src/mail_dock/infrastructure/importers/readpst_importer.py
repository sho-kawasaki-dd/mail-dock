"""Concrete archive importer for the bundled readpst/lspst tools."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from mail_dock.domain.errors import ConverterNotFound
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.importer import (
    ArchiveInfo,
    BaseArchiveImporter,
    ExtractResult,
    ImportOptions,
)
from mail_dock.infrastructure.importers.lspst_parser import (
    detect_pst_format,
    parse_lspst_output,
)
from mail_dock.infrastructure.importers.readpst_locator import ReadPstLocator
from mail_dock.infrastructure.importers.readpst_runner import ReadPstRunner
from mail_dock.infrastructure.storage.pst_import_storage import PstImportStorage


class ReadPstImporter(BaseArchiveImporter):
    """Use readpst for extraction and lspst only for advisory probe data."""

    def __init__(self, locator: ReadPstLocator | None = None) -> None:
        self._locator = locator or ReadPstLocator()
        self._storage = PstImportStorage()

    def get_version(self) -> str:
        """Return the validated bundled converter version."""

        return self._locator.get_version()

    def probe(
        self,
        source: Path,
        *,
        cancel: CancelToken,
        on_progress: Callable[[int], None],
    ) -> ArchiveInfo:
        source = source.expanduser().resolve()
        cancel.raise_if_cancelled()
        snapshot = self._storage.snapshot_source_file(source, cancel=cancel)
        folders = []
        try:
            lspst = self._locator.resolve_lspst()
            result = subprocess.run(
                [str(lspst), str(source)],
                cwd=lspst.parent,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                text=False,
            )
            cancel.raise_if_cancelled()
            folders = parse_lspst_output(result.stdout)
        except (ConverterNotFound, OSError, subprocess.SubprocessError):
            folders = []
        on_progress(len(folders))
        return ArchiveInfo(
            format=detect_pst_format(source),
            folders=folders,
            estimated_total=None,
            source_sha256=snapshot.source_sha256,
            source_size_bytes=snapshot.size_bytes,
        )

    def extract(
        self,
        source: Path,
        staging: Path,
        options: ImportOptions,
        *,
        cancel: CancelToken,
        on_progress: Callable[[int], None],
    ) -> ExtractResult:
        runner = ReadPstRunner(self._locator.resolve_readpst())
        return runner.extract(
            source,
            staging,
            options,
            cancel=cancel,
            on_progress=on_progress,
        )


__all__ = ["ReadPstImporter"]
