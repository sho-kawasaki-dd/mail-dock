"""Review and confirmation dialogs for remote message deletion."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import override

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    Qt,
    Signal,
)
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mail_dock.domain.search import MessageFilter
from mail_dock.presentation import strings
from mail_dock.usecases.delete_remote import DeleteDryRunResult, DeleteScope

_EMPTY_MODEL_INDEX = QModelIndex()


def _display_size(size_bytes: int) -> str:
    return f"{size_bytes:,}"


class DeleteByListOptionsDialog(QDialog):
    """Confirm the fixed list conditions and protection options."""

    def __init__(
        self,
        *,
        folder_name: str,
        query: str,
        mode: str,
        filters: MessageFilter,
        delete_batch_limit: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(strings.DIALOG_REMOTE_DELETE_LIST_OPTIONS_TITLE)
        self.setMinimumWidth(480)

        form = QFormLayout(self)
        form.addRow(QLabel(strings.DIALOG_REMOTE_DELETE_LIST_CONDITIONS, self))
        form.addRow(strings.DIALOG_REMOTE_DELETE_LIST_FOLDER, QLabel(folder_name, self))
        form.addRow(
            strings.DIALOG_REMOTE_DELETE_LIST_DATE_FROM,
            QLabel(_display_filter_date(filters.date_from), self),
        )
        form.addRow(
            strings.DIALOG_REMOTE_DELETE_LIST_DATE_TO,
            QLabel(_display_filter_date(filters.date_to), self),
        )
        form.addRow(
            strings.DIALOG_REMOTE_DELETE_LIST_QUERY,
            QLabel(query or strings.DIALOG_REMOTE_DELETE_LIST_NO_VALUE, self),
        )
        form.addRow(
            strings.DIALOG_REMOTE_DELETE_LIST_MODE,
            QLabel(
                strings.DIALOG_REMOTE_DELETE_LIST_MODE_OR
                if mode == "or"
                else strings.DIALOG_REMOTE_DELETE_LIST_MODE_AND,
                self,
            ),
        )
        attachment = {
            True: strings.DIALOG_REMOTE_DELETE_LIST_ATTACHMENT_YES,
            False: strings.DIALOG_REMOTE_DELETE_LIST_ATTACHMENT_NO,
            None: strings.DIALOG_REMOTE_DELETE_LIST_ATTACHMENT_ALL,
        }[filters.has_attachment]
        form.addRow(strings.DIALOG_REMOTE_DELETE_LIST_ATTACHMENT, QLabel(attachment, self))
        form.addRow(
            strings.DIALOG_REMOTE_DELETE_LIST_LIMIT,
            QLabel(f"{delete_batch_limit:,} 件", self),
        )

        self._exclude_flagged = QCheckBox(
            strings.DIALOG_REMOTE_DELETE_LIST_EXCLUDE_FLAGGED,
            self,
        )
        self._exclude_flagged.setChecked(True)
        self._flagged_warning = QLabel(
            strings.DIALOG_REMOTE_DELETE_LIST_FLAGGED_WARNING,
            self,
        )
        self._flagged_warning.setWordWrap(True)
        self._flagged_warning.setVisible(False)
        self._exclude_flagged.toggled.connect(self._update_flagged_warning)
        form.addRow(self._exclude_flagged)
        form.addRow(self._flagged_warning)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    @property
    def exclude_flagged(self) -> bool:
        return self._exclude_flagged.isChecked()

    def _update_flagged_warning(self, exclude_flagged: bool) -> None:
        self._flagged_warning.setVisible(not exclude_flagged)


def _display_filter_date(value: object) -> str:
    if value is None:
        return strings.DIALOG_REMOTE_DELETE_LIST_NO_VALUE
    formatter = getattr(value, "strftime", None)
    return formatter("%Y-%m-%d") if callable(formatter) else str(value)


class _DeleteDryRunTableModel(QAbstractTableModel):
    """Generate only requested cells for large list-wide dry-run results."""

    HEADERS = (
        strings.DIALOG_REMOTE_DELETE_DRY_RUN_KIND,
        strings.DIALOG_REMOTE_DELETE_DRY_RUN_SUBJECT,
        strings.DIALOG_REMOTE_DELETE_DRY_RUN_DATE,
        strings.DIALOG_REMOTE_DELETE_DRY_RUN_SIZE,
        strings.DIALOG_REMOTE_DELETE_DRY_RUN_REASON,
    )

    def __init__(self, result: DeleteDryRunResult, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._candidates = result.candidates
        self._exclusions = result.exclusions

    @override
    def rowCount(
        self,
        parent: QModelIndex | QPersistentModelIndex = _EMPTY_MODEL_INDEX,
    ) -> int:
        return 0 if parent.isValid() else len(self._candidates) + len(self._exclusions)

    @override
    def columnCount(
        self,
        parent: QModelIndex | QPersistentModelIndex = _EMPTY_MODEL_INDEX,
    ) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    @override
    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation is Qt.Orientation.Horizontal and 0 <= section < len(self.HEADERS):
            return self.HEADERS[section]
        return None

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if role != Qt.ItemDataRole.DisplayRole or not index.isValid():
            return None
        row = index.row()
        column = index.column()
        if row < len(self._candidates):
            candidate = self._candidates[row]
            values = (
                strings.DIALOG_REMOTE_DELETE_DRY_RUN_INCLUDED,
                candidate.subject,
                candidate.date or "",
                _display_size(candidate.size_bytes),
                "",
            )
        else:
            exclusion = self._exclusions[row - len(self._candidates)]
            values = (
                strings.DIALOG_REMOTE_DELETE_DRY_RUN_EXCLUDED,
                exclusion.subject,
                "",
                _display_size(exclusion.size_bytes),
                _display_reason(exclusion.reason),
            )
        return values[column] if 0 <= column < len(values) else None


def _display_reason(reason: str) -> str:
    localized = {
        "remote_state_not_deletable": strings.DIALOG_REMOTE_DELETE_REASON_REMOTE_STATE,
        "flagged": strings.DIALOG_REMOTE_DELETE_REASON_FLAGGED,
        "flagged_on_server": strings.DIALOG_REMOTE_DELETE_REASON_FLAGGED_ON_SERVER,
        "flag_unverified": strings.DIALOG_REMOTE_DELETE_REASON_FLAG_UNVERIFIED,
        "uidvalidity_mismatch": strings.DIALOG_REMOTE_DELETE_REASON_UIDVALIDITY_MISMATCH,
    }
    return localized.get(reason, reason)


class DeleteDryRunDialog(QDialog):
    """Show the exact remote-delete plan before asking for confirmation."""

    csv_saved = Signal(str)

    def __init__(self, result: DeleteDryRunResult, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._result = result
        self.setWindowTitle(strings.DIALOG_REMOTE_DELETE_DRY_RUN_TITLE)
        self.resize(900, 520)

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

        self._table: QTableView | QTableWidget
        if result.scope is None:
            self._table = QTableWidget(0, 5, self)
            self._table.setHorizontalHeaderLabels(_DeleteDryRunTableModel.HEADERS)
            self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
            self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            self._add_rows()
            self._table.resizeColumnsToContents()
        else:
            self._add_scope_summary(layout, result.scope)
            table = QTableView(self)
            table.setModel(_DeleteDryRunTableModel(result, table))
            table.setSelectionMode(QTableView.SelectionMode.NoSelection)
            table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
            table.setColumnWidth(0, 90)
            table.setColumnWidth(1, 300)
            table.setColumnWidth(2, 130)
            table.setColumnWidth(3, 110)
            table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            table.horizontalHeader().setStretchLastSection(True)
            self._table = table
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
    def table(self) -> QTableView | QTableWidget:
        """Expose the review table for presentation tests."""

        return self._table

    def _add_scope_summary(self, layout: QVBoxLayout, scope: DeleteScope) -> None:
        counts: dict[str, int] = {}
        for exclusion in self._result.exclusions:
            counts[exclusion.reason] = counts.get(exclusion.reason, 0) + 1
        state_excluded = counts.get("remote_state_not_deletable", 0)
        flagged_excluded = counts.get("flagged", 0)
        other_excluded = self._result.excluded_count - state_excluded - flagged_excluded
        truncated = (
            scope.matched_count
            - len(scope.non_deletable_message_ids)
            - len(scope.flagged_message_ids)
            - len(scope.message_ids)
        )
        layout.addWidget(
            QLabel(
                strings.DIALOG_REMOTE_DELETE_DRY_RUN_COUNTS.format(
                    matched=scope.matched_count,
                    state_excluded=state_excluded,
                    flagged_excluded=flagged_excluded,
                    truncated=truncated,
                    other_excluded=other_excluded,
                    candidates=self._result.candidate_count,
                ),
                self,
            )
        )
        if scope.truncated:
            warning = QLabel(strings.DIALOG_REMOTE_DELETE_DRY_RUN_TRUNCATED_WARNING, self)
            warning.setWordWrap(True)
            warning.setStyleSheet("color: #9a6700;")
            layout.addWidget(warning)

    def _add_rows(self) -> None:
        if not isinstance(self._table, QTableWidget):
            return
        for candidate in self._result.candidates:
            self._add_row(
                self._table,
                strings.DIALOG_REMOTE_DELETE_DRY_RUN_INCLUDED,
                candidate.subject,
                candidate.date or "",
                candidate.size_bytes,
                "",
            )
        for exclusion in self._result.exclusions:
            self._add_row(
                self._table,
                strings.DIALOG_REMOTE_DELETE_DRY_RUN_EXCLUDED,
                exclusion.subject,
                "",
                exclusion.size_bytes,
                _display_reason(exclusion.reason),
            )

    def _add_row(
        self,
        table: QTableWidget,
        kind: str,
        subject: str,
        date: str,
        size: int,
        reason: str,
    ) -> None:
        row = table.rowCount()
        table.insertRow(row)
        for column, value in enumerate((kind, subject, date, _display_size(size), reason)):
            table.setItem(row, column, QTableWidgetItem(value))

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
