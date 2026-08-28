"""Read-only paginated viewer for the permanent audit log (D-22 / F-40)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mail_dock.domain.masking import mask_emails, mask_subject
from mail_dock.domain.repository import MessageRecord
from mail_dock.presentation import strings


class _AuditLogWorker(Protocol):
    audit_log_result: Any
    error_reported: Any

    def list_audit_log(self, limit: int, offset: int) -> object: ...


class AuditLogDialog(QDialog):
    """Show ``audit_log`` newest-first with no delete or edit affordance."""

    _PAGE_SIZE = 50
    _COLUMNS = (
        strings.AUDIT_LOG_COLUMN_OCCURRED_AT,
        strings.AUDIT_LOG_COLUMN_OPERATION,
        strings.AUDIT_LOG_COLUMN_ACCOUNT,
        strings.AUDIT_LOG_COLUMN_MESSAGE_ID,
        strings.AUDIT_LOG_COLUMN_SUBJECT,
        strings.AUDIT_LOG_COLUMN_SIZE,
        strings.AUDIT_LOG_COLUMN_DETAIL,
    )

    def __init__(self, worker: _AuditLogWorker, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._worker = worker
        self._offset = 0
        self._pending_offset = 0
        self._row_count = 0
        self._build_ui()
        self._worker.audit_log_result.connect(self._show_page)
        self._worker.error_reported.connect(self._show_worker_error)
        self._load_page(0)

    @property
    def table(self) -> QTableWidget:
        """Expose the table for presentation tests."""

        return self._table

    @property
    def previous_button(self) -> QPushButton:
        """Expose the previous-page action for presentation tests."""

        return self._previous_button

    @property
    def next_button(self) -> QPushButton:
        """Expose the next-page action for presentation tests."""

        return self._next_button

    @property
    def status_label(self) -> QLabel:
        return self._status_label

    def _build_ui(self) -> None:
        self.setWindowTitle(strings.AUDIT_LOG_TITLE)
        self.resize(900, 480)
        layout = QVBoxLayout(self)

        self._status_label = QLabel(strings.AUDIT_LOG_STATUS_LOADING, self)
        layout.addWidget(self._status_label)

        self._table = QTableWidget(0, len(self._COLUMNS), self)
        self._table.setHorizontalHeaderLabels(self._COLUMNS)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self._table, 1)

        pager = QHBoxLayout()
        self._previous_button = QPushButton(strings.AUDIT_LOG_PREVIOUS, self)
        self._next_button = QPushButton(strings.AUDIT_LOG_NEXT, self)
        self._previous_button.setEnabled(False)
        self._next_button.setEnabled(False)
        pager.addWidget(self._previous_button)
        pager.addWidget(self._next_button)
        pager.addStretch(1)
        layout.addLayout(pager)

        buttons = QDialogButtonBox(parent=self)
        self._close_button = QPushButton(strings.AUDIT_LOG_CLOSE, self)
        buttons.addButton(self._close_button, QDialogButtonBox.ButtonRole.RejectRole)
        layout.addWidget(buttons)

        self._previous_button.clicked.connect(self._show_previous_page)
        self._next_button.clicked.connect(self._show_next_page)
        self._close_button.clicked.connect(self.reject)

    def _load_page(self, offset: int) -> None:
        self._pending_offset = offset
        self._previous_button.setEnabled(False)
        self._next_button.setEnabled(False)
        self._status_label.setText(strings.AUDIT_LOG_STATUS_LOADING)
        self._worker.list_audit_log(self._PAGE_SIZE, offset)

    def _show_previous_page(self) -> None:
        self._load_page(max(0, self._offset - self._PAGE_SIZE))

    def _show_next_page(self) -> None:
        self._load_page(self._offset + self._PAGE_SIZE)

    def _show_page(self, entries: object) -> None:
        if not isinstance(entries, Sequence):
            return
        self._offset = self._pending_offset
        self._row_count = len(entries)
        self._populate(entries)
        self._previous_button.setEnabled(self._offset > 0)
        self._next_button.setEnabled(self._row_count == self._PAGE_SIZE)
        if self._row_count:
            self._status_label.setText(
                strings.AUDIT_LOG_STATUS_PAGE.format(
                    start=self._offset + 1, end=self._offset + self._row_count
                )
            )
        else:
            self._status_label.setText(strings.AUDIT_LOG_EMPTY)

    def _populate(self, entries: Sequence[MessageRecord]) -> None:
        self._table.setRowCount(0)
        for entry in entries:
            row = self._table.rowCount()
            self._table.insertRow(row)
            subject = entry.get("subject")
            values = (
                entry.get("occurred_at") or "",
                entry.get("operation") or "",
                mask_emails(str(entry.get("account_id") or "")),
                entry.get("message_id") or "",
                mask_subject(str(subject)) if subject else "",
                entry.get("size_bytes") if entry.get("size_bytes") is not None else "",
                mask_emails(str(entry.get("detail") or "")),
            )
            for column, value in enumerate(values):
                self._table.setItem(row, column, QTableWidgetItem(str(value)))
        self._table.resizeColumnsToContents()

    def _show_worker_error(self, notification: object) -> None:
        operation = getattr(notification, "operation", None)
        if operation != "audit_log":
            return
        self._status_label.setText(str(getattr(notification, "message", notification)))
