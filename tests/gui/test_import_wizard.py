from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from PySide6.QtCore import QObject, Signal

from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.importer import ArchiveFolder, ArchiveInfo
from mail_dock.presentation.views.import_wizard import ImportWizard

pytestmark = pytest.mark.gui


class _Importer:
    def probe(self, source: Path, *, cancel: CancelToken, on_progress: Any) -> ArchiveInfo:
        cancel.raise_if_cancelled()
        on_progress(1)
        return ArchiveInfo(
            format="unicode",
            folders=[ArchiveFolder("Inbox", "Inbox")],
            estimated_total=1,
            source_sha256="a" * 64,
            source_size_bytes=source.stat().st_size,
        )


class _Context:
    storage_root = None

    @staticmethod
    def create_pst_importer() -> _Importer:
        return _Importer()


class _SyncWorker(QObject):
    progress = Signal(object)
    pst_import_result = Signal(object)
    pst_import_decision_required = Signal(object)
    pst_import_cancelled = Signal(object)
    error_reported = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict[str, object]] = []

    def import_pst(self, source: Path, **options: object) -> CancelToken:
        self.calls.append({"source": source, **options})
        return CancelToken()


def test_probe_runs_before_progress_and_cancel_stops_import(qtbot: Any, tmp_path: Path) -> None:
    source = tmp_path / "archive.pst"
    source.write_bytes(b"pst")
    sync_worker = _SyncWorker()
    wizard = ImportWizard(cast(Any, _Context()), cast(Any, sync_worker))
    qtbot.addWidget(wizard)
    wizard._source_edit.setText(str(source))
    wizard._probe()

    qtbot.waitUntil(lambda: wizard._info is not None, timeout=3000)
    assert wizard._source_page.isComplete()
    assert wizard._display_name.text() == "archive"

    assert len(wizard.pageIds()) == 4
    wizard._start_import()
    assert len(sync_worker.calls) == 1
    assert sync_worker.calls[0]["source"] == source.resolve()

    wizard.reject()
    assert wizard._import_token is None
