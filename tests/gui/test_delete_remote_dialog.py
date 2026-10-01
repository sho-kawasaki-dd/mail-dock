from __future__ import annotations

import csv
import time
import tracemalloc
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtWidgets import QApplication, QCheckBox, QLabel, QTableView, QTableWidget

from mail_dock.domain.search import MessageFilter
from mail_dock.presentation.views.dialogs.delete_remote_dialog import (
    DeleteByListOptionsDialog,
    DeleteConfirmationDialog,
    DeleteDryRunDialog,
)
from mail_dock.usecases.delete_remote import (
    DeleteCandidate,
    DeleteDryRunResult,
    DeleteExclusion,
    DeleteScope,
)

pytestmark = pytest.mark.gui


def _candidate() -> DeleteCandidate:
    return DeleteCandidate(
        message_id=1,
        account_id="account-1",
        folder_raw_name="INBOX",
        uid=42,
        uidvalidity=7,
        subject="Subject",
        date_sent="2026-01-02",
        internal_date=None,
        size_bytes=128,
        relative_path="eml/2026/01/message.eml",
        file_hash="a" * 64,
        message_id_header="<message@example.com>",
    )


def _result() -> DeleteDryRunResult:
    return DeleteDryRunResult(
        candidates=(_candidate(),),
        exclusions=(
            DeleteExclusion(
                message_id=2,
                reason="hash_mismatch",
                subject="Excluded subject",
                size_bytes=64,
            ),
        ),
        total_size_bytes=128,
    )


def test_dry_run_dialog_shows_candidates_exclusions_and_total(qtbot: Any) -> None:
    dialog = DeleteDryRunDialog(_result())
    qtbot.addWidget(dialog)

    assert dialog.table.rowCount() == 2
    candidate_subject = dialog.table.item(0, 1)
    exclusion_subject = dialog.table.item(1, 1)
    exclusion_reason = dialog.table.item(1, 4)
    assert candidate_subject is not None
    assert exclusion_subject is not None
    assert exclusion_reason is not None
    assert candidate_subject.text() == "Subject"
    assert exclusion_subject.text() == "Excluded subject"
    assert exclusion_reason.text() == "hash_mismatch"
    assert any("128" in label.text() for label in dialog.findChildren(QLabel))


def test_dry_run_csv_contains_audit_fields_only(
    qtbot: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "dry-run.csv"
    result = replace(
        _result(),
        scope=DeleteScope(
            message_ids=(1,),
            flagged_message_ids=(2,),
            non_deletable_message_ids=(),
            matched_count=2,
            flagged_excluded_count=1,
            truncated=False,
            delete_batch_limit=1000,
        ),
    )
    monkeypatch.setattr(
        "mail_dock.presentation.views.dialogs.delete_remote_dialog.QFileDialog.getSaveFileName",
        lambda *_args, **_kwargs: (str(destination), "csv"),
    )
    dialog = DeleteDryRunDialog(result)
    qtbot.addWidget(dialog)

    dialog._save_csv()

    with destination.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.reader(stream))
    assert rows == [
        ["kind", "subject", "date", "size_bytes", "reason"],
        ["included", "Subject", "2026-01-02", "128", ""],
        ["excluded", "Excluded subject", "", "64", "hash_mismatch"],
        ["total", "", "", "128", ""],
    ]
    assert "message@example.com" not in destination.read_text(encoding="utf-8-sig")


def test_list_options_default_to_flagged_exclusion_and_warn_when_disabled(qtbot: Any) -> None:
    dialog = DeleteByListOptionsDialog(
        folder_name="受信箱",
        query="請求書",
        mode="or",
        filters=MessageFilter(
            date_from=datetime(2026, 1, 1, tzinfo=UTC),
            date_to=datetime(2026, 2, 1, tzinfo=UTC),
            has_attachment=False,
        ),
        delete_batch_limit=1000,
    )
    qtbot.addWidget(dialog)

    checkbox = dialog.findChild(QCheckBox)
    assert checkbox is not None and checkbox.isChecked()
    assert dialog.exclude_flagged
    warning = next(
        label
        for label in dialog.findChildren(QLabel)
        if "スター付きメールも削除対象" in label.text()
    )
    assert warning.isHidden()
    checkbox.setChecked(False)
    assert not dialog.exclude_flagged
    assert not warning.isHidden()
    label_text = " ".join(label.text() for label in dialog.findChildren(QLabel))
    assert "受信箱" in label_text
    assert "請求書" in label_text
    assert "1,000" in label_text
    assert "添付なし" in label_text


def test_scope_dry_run_uses_lazy_table_model_and_localizes_exclusion(qtbot: Any) -> None:
    result = replace(
        _result(),
        exclusions=(
            DeleteExclusion(message_id=2, reason="flagged_on_server", subject="Flagged"),
        ),
        scope=DeleteScope(
            message_ids=(1,),
            flagged_message_ids=(2,),
            non_deletable_message_ids=(),
            matched_count=2,
            flagged_excluded_count=1,
            truncated=False,
            delete_batch_limit=1000,
        ),
    )
    dialog = DeleteDryRunDialog(result)
    qtbot.addWidget(dialog)

    assert isinstance(dialog.table, QTableView)
    assert not dialog.findChildren(QTableWidget)
    model = dialog.table.model()
    assert model is not None and model.rowCount() == 2
    assert model.data(model.index(0, 1)) == "Subject"
    assert model.data(model.index(1, 4)) == "サーバーでスター付きのため除外"


def test_scope_table_retains_large_result_without_prebuilding_cells(qtbot: Any) -> None:
    candidate_count = 1000
    exclusion_count = 100_000
    candidates = tuple(
        replace(_candidate(), message_id=index + exclusion_count, subject=f"Candidate {index}")
        for index in range(candidate_count)
    )
    exclusions = tuple(
        DeleteExclusion(message_id=index, reason="flagged", subject=f"Excluded {index}")
        for index in range(exclusion_count)
    )
    result = DeleteDryRunResult(
        candidates=candidates,
        exclusions=exclusions,
        total_size_bytes=candidate_count * 128,
        exclude_flagged=True,
        scope=DeleteScope(
            message_ids=tuple(candidate.message_id for candidate in candidates),
            flagged_message_ids=tuple(range(exclusion_count)),
            non_deletable_message_ids=(),
            matched_count=candidate_count + exclusion_count,
            flagged_excluded_count=exclusion_count,
            truncated=False,
            delete_batch_limit=candidate_count,
        ),
    )
    tracemalloc.start()
    started = time.perf_counter()
    dialog = DeleteDryRunDialog(result)
    qtbot.addWidget(dialog)
    dialog.show()
    QApplication.processEvents()
    elapsed_seconds = time.perf_counter() - started
    _current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(
        "scope table initialization: "
        f"{elapsed_seconds:.3f}s, Python heap peak {peak_bytes / 1024 / 1024:.2f} MiB"
    )

    assert isinstance(dialog.table, QTableView)
    model = dialog.table.model()
    assert model is not None
    assert model.rowCount() == candidate_count + exclusion_count
    assert model.data(model.index(0, 1)) == "Candidate 0"
    assert model.data(model.index(model.rowCount() - 1, 1)) == f"Excluded {exclusion_count - 1}"
    assert dialog.table.columnWidth(0) == 90
    dialog.table.verticalScrollBar().setValue(dialog.table.verticalScrollBar().maximum())
    QApplication.processEvents()
    dialog.close()
    assert not dialog.isVisible()


def test_confirmation_dialog_requires_matching_count(qtbot: Any) -> None:
    dialog = DeleteConfirmationDialog(_result())
    qtbot.addWidget(dialog)

    assert not dialog._ok_button.isEnabled()
    dialog.count_edit.setText("2")
    assert not dialog._ok_button.isEnabled()
    dialog.count_edit.setText("1")
    assert dialog._ok_button.isEnabled()
