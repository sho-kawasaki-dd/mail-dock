"""Provider-independent contracts for one-time archive imports."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mail_dock.domain.fetcher import CancelToken


@dataclass(frozen=True)
class ArchiveFolder:
    """A folder discovered below an archive extraction staging root."""

    relative_path: str
    display_name: str
    estimated_count: int | None = None


@dataclass(frozen=True)
class ArchiveInfo:
    """Archive metadata discovered without extracting its messages."""

    format: str
    folders: list[ArchiveFolder]
    estimated_total: int | None
    source_sha256: str
    source_size_bytes: int


@dataclass(frozen=True)
class ExtractResult:
    """Result of extracting an archive into a staging directory."""

    staging_root: Path
    file_count: int
    stdout_tail: str
    stderr_tail: str


@dataclass(frozen=True)
class ImportOptions:
    """User-selected options controlling archive extraction."""

    display_name: str
    charset: str
    include_deleted: bool = False


class BaseArchiveImporter(ABC):
    """Contract for one-time archive conversion and extraction.

    Archive importers must not share the live-connection contract of
    ``BaseMailFetcher``.  In particular, extraction is staged and can be
    resumed by the use-case layer after the extractor process has finished.
    """

    @abstractmethod
    def probe(
        self,
        source: Path,
        *,
        cancel: CancelToken,
        on_progress: Callable[[int], None],
    ) -> ArchiveInfo:
        """Inspect an archive and return best-effort metadata."""

    @abstractmethod
    def extract(
        self,
        source: Path,
        staging: Path,
        options: ImportOptions,
        *,
        cancel: CancelToken,
        on_progress: Callable[[int], None],
    ) -> ExtractResult:
        """Extract archive messages into the supplied staging directory."""
