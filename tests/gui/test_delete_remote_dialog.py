from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtWidgets import QLabel

from mail_dock.presentation.views.dialogs.delete_remote_dialog import (
    DeleteConfirmationDialog,
    DeleteDryRunDialog,
)
from mail_dock.usecases.delete_remote import (
    DeleteCandidate,
    DeleteDryRunResult,
    DeleteExclusion,
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
    monkeypatch.setattr(
        "mail_dock.presentation.views.dialogs.delete_remote_dialog.QFileDialog.getSaveFileName",
        lambda *_args, **_kwargs: (str(destination), "csv"),
    )
    dialog = DeleteDryRunDialog(_result())
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


def test_confirmation_dialog_requires_matching_count(qtbot: Any) -> None:
    dialog = DeleteConfirmationDialog(_result())
    qtbot.addWidget(dialog)

    assert not dialog._ok_button.isEnabled()
    dialog.count_edit.setText("2")
    assert not dialog._ok_button.isEnabled()
    dialog.count_edit.setText("1")
    assert dialog._ok_button.isEnabled()
