from __future__ import annotations

from typing import Any

import pytest
from PySide6.QtCore import QObject, Signal

from mail_dock.presentation import strings
from mail_dock.presentation.views.dialogs.integrity_dialog import IntegrityDialog
from mail_dock.usecases.verify import QuickVerifyResult, VerifyProgress

pytestmark = pytest.mark.gui


class _Worker(QObject):
    verify_progress = Signal(object)
    verify_result = Signal(object)
    error_reported = Signal(object)
    cancelled = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []
        self.cancel_calls = 0

    def quick_verify(self) -> object:
        self.calls.append("quick")
        return object()

    def range_verify(self) -> object:
        self.calls.append("range")
        return object()

    def full_verify(self) -> object:
        self.calls.append("full")
        return object()

    def orphan_scan(self) -> object:
        self.calls.append("orphans")
        return object()

    def verify_manifest(self) -> object:
        self.calls.append("manifest")
        return object()

    def reindex(self) -> object:
        self.calls.append("reindex")
        return object()

    def reparse(self, *, only_failed: bool = True) -> object:
        del only_failed
        self.calls.append("reparse")
        return object()

    def cancel_all(self) -> None:
        self.cancel_calls += 1


def test_integrity_dialog_runs_selected_mode_and_lists_issues(qtbot: Any) -> None:
    worker = _Worker()
    dialog = IntegrityDialog(worker)
    qtbot.addWidget(dialog)

    modes = {dialog.mode_selector.itemData(index) for index in range(dialog.mode_selector.count())}
    assert modes == {"quick", "range", "full", "orphans", "manifest", "reindex", "reparse"}
    assert dialog.start_mode("quick")
    assert worker.calls == ["quick"]

    worker.verify_progress.emit(VerifyProgress(1, 2, "eml/current.eml"))
    assert dialog.progress_bar.value() == 1
    worker.verify_result.emit(QuickVerifyResult(2, ("eml/missing.eml",), (), False))

    assert dialog.results_list.count() == 1
    assert "eml/missing.eml" in dialog.results_list.item(0).text()
    assert "問題 1 件" in dialog.status_label.text()


def test_reindex_requires_confirmation_and_cancel_requests_worker_stop(qtbot: Any) -> None:
    worker = _Worker()
    confirmations: list[str] = []

    def reject_reindex(message: str, _parent: Any) -> bool:
        confirmations.append(message)
        return False

    dialog = IntegrityDialog(
        worker,
        confirmation=reject_reindex,
    )
    qtbot.addWidget(dialog)

    assert not dialog.start_mode("reindex")
    assert worker.calls == []
    assert confirmations == [strings.INTEGRITY_CONFIRM_REINDEX]

    dialog.start_mode("full")
    dialog.cancel_button.click()
    assert worker.cancel_calls == 1
