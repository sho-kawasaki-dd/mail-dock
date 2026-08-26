"""Review and confirmation dialogs for remote message deletion."""

from __future__ import annotations

import csv
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mail_dock.presentation import strings
from mail_dock.usecases.delete_remote import DeleteDryRunResult


def _display_size(size_bytes: int) -> str:
    return f"{size_bytes:,}"


class DeleteDryRunDialog(QDialog):
    """Show the exact remote-delete plan before asking for confirmation."""

    csv_saved = Signal(str)

    def __init__(self, result: DeleteDryRunResult, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._result = result
        self.setWindowTitle(strings.DIALOG_REMOTE_DELETE_DRY_RUN_TITLE)
        self.resize(760, 420)

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                strings.DIALOG_REMOTE_DELETE_DRY_RUN_TOTAL.format(
                    count=result.candidate_count,
                    size=_display_size(result.total_size_bytes),
                ),
                self,
            )
        )

        self._table = QTableWidget(0, 5, self)
        self._table.setHorizontalHeaderLabels(
            (
                "区分",
                strings.DIALOG_REMOTE_DELETE_DRY_RUN_SUBJECT,
                strings.DIALOG_REMOTE_DELETE_DRY_RUN_DATE,
                strings.DIALOG_REMOTE_DELETE_DRY_RUN_SIZE,
                strings.DIALOG_REMOTE_DELETE_DRY_RUN_REASON,
            )
        )
        self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._add_rows()
        self._table.resizeColumnsToContents()
        layout.addWidget(self._table, 1)

        controls = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel, parent=self)
        continue_button = QPushButton(strings.DIALOG_REMOTE_DELETE_DRY_RUN_CONTINUE, self)
        continue_button.setEnabled(bool(result.candidates))
        continue_button.clicked.connect(self.accept)
        controls.addButton(continue_button, QDialogButtonBox.ButtonRole.AcceptRole)
        save_button = QPushButton(strings.DIALOG_REMOTE_DELETE_DRY_RUN_SAVE_CSV, self)
        save_button.clicked.connect(self._save_csv)
        controls.addButton(save_button, QDialogButtonBox.ButtonRole.ActionRole)
        controls.rejected.connect(self.reject)
        layout.addWidget(controls)

    @property
    def table(self) -> QTableWidget:
        """Expose the review table for presentation tests."""

        return self._table

    def _add_rows(self) -> None:
        for candidate in self._result.candidates:
            self._add_row(
                strings.DIALOG_REMOTE_DELETE_DRY_RUN_INCLUDED,
                candidate.subject,
                candidate.date or "",
                candidate.size_bytes,
                "",
            )
        for exclusion in self._result.exclusions:
            self._add_row(
                strings.DIALOG_REMOTE_DELETE_DRY_RUN_EXCLUDED,
                exclusion.subject,
                "",
                exclusion.size_bytes,
                exclusion.reason,
            )

    def _add_row(self, kind: str, subject: str, date: str, size: int, reason: str) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        for column, value in enumerate((kind, subject, date, _display_size(size), reason)):
            self._table.setItem(row, column, QTableWidgetItem(value))

    def _save_csv(self) -> None:
        selected, _ = QFileDialog.getSaveFileName(
            self,
            strings.DIALOG_REMOTE_DELETE_CSV_TITLE,
            "remote-delete-dry-run.csv",
            strings.DIALOG_REMOTE_DELETE_CSV_FILTER,
        )
        if not selected:
            return
        path = Path(selected)
        try:
            with path.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(("kind", "subject", "date", "size_bytes", "reason"))
                for candidate in self._result.candidates:
                    writer.writerow(
                        (
                            "included",
                            candidate.subject,
                            candidate.date or "",
                            candidate.size_bytes,
                            "",
                        )
                    )
                for exclusion in self._result.exclusions:
                    writer.writerow(
                        (
                            "excluded",
                            exclusion.subject,
                            "",
                            exclusion.size_bytes,
                            exclusion.reason,
                        )
                    )
                writer.writerow(("total", "", "", self._result.total_size_bytes, ""))
        except OSError:
            return
        self.csv_saved.emit(str(path))


class DeleteConfirmationDialog(QDialog):
    """Require the user to type the reviewed candidate count."""

    def __init__(
        self,
        result: DeleteDryRunResult,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._expected_count = result.candidate_count
        self.setWindowTitle(strings.DIALOG_REMOTE_DELETE_CONFIRM_TITLE)

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                strings.DIALOG_REMOTE_DELETE_CONFIRM_MESSAGE.format(
                    count=result.candidate_count,
                    size=_display_size(result.total_size_bytes),
                ),
                self,
            )
        )
        self._count_edit = QLineEdit(self)
        self._count_edit.setPlaceholderText(strings.DIALOG_REMOTE_DELETE_CONFIRM_PLACEHOLDER)
        self._count_edit.setValidator(QIntValidator(0, 2**31 - 1, self))
        self._count_edit.textChanged.connect(self._validate_count)
        layout.addWidget(self._count_edit)
        self._error_label = QLabel(self)
        self._error_label.setStyleSheet("color: #b42318;")
        layout.addWidget(self._error_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self._ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok_button.setEnabled(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def count_edit(self) -> QLineEdit:
        """Expose the count field for presentation tests."""

        return self._count_edit

    def _validate_count(self, text: str) -> None:
        try:
            valid = int(text) == self._expected_count
        except ValueError:
            valid = False
        self._ok_button.setEnabled(valid)
        self._error_label.setText(
            "" if not text or valid else strings.DIALOG_REMOTE_DELETE_CONFIRM_COUNT_MISMATCH
        )
