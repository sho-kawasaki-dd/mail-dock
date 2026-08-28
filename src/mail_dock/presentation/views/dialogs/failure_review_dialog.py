"""Manual review dialog for failures that exhausted automatic retries."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mail_dock.presentation import strings


class FailureReviewDialog(QDialog):
    """Display exhausted failures and expose only applicable manual actions."""

    fetch_requested = Signal(object)
    reparse_requested = Signal(object)

    _COLUMNS = (
        strings.FAILURE_REVIEW_TYPE,
        strings.FAILURE_REVIEW_SUBJECT,
        strings.FAILURE_REVIEW_FOLDER,
        strings.FAILURE_REVIEW_UID,
        strings.FAILURE_REVIEW_ATTEMPTS,
        strings.FAILURE_REVIEW_LAST_FAILED,
    )

    def __init__(
        self,
        failures: Sequence[Mapping[str, Any]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._failures = tuple(dict(failure) for failure in failures)
        self.setWindowTitle(strings.FAILURE_REVIEW_TITLE)
        self.resize(900, 420)

        layout = QVBoxLayout(self)
        self._table = QTableWidget(0, len(self._COLUMNS), self)
        self._table.setHorizontalHeaderLabels(self._COLUMNS)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._populate()
        layout.addWidget(self._table, 1)

        buttons = QDialogButtonBox(parent=self)
        self._fetch_button = QPushButton(strings.FAILURE_REVIEW_FETCH, self)
        self._reparse_button = QPushButton(strings.FAILURE_REVIEW_REPARSE, self)
        self._close_button = QPushButton(strings.FAILURE_REVIEW_CLOSE, self)
        buttons.addButton(self._fetch_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self._reparse_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self._close_button, QDialogButtonBox.ButtonRole.RejectRole)
        layout.addWidget(buttons)
        self._fetch_button.clicked.connect(self._request_fetch)
        self._reparse_button.clicked.connect(self._request_reparse)
        self._close_button.clicked.connect(self.reject)
        self._table.itemSelectionChanged.connect(self._update_actions)
        self._update_actions()

    @property
    def table(self) -> QTableWidget:
        """Expose the table for presentation tests."""

        return self._table

    @property
    def fetch_button(self) -> QPushButton:
        """Expose the oversized-message action for presentation tests."""

        return self._fetch_button

    @property
    def reparse_button(self) -> QPushButton:
        """Expose the parse-failure action for presentation tests."""

        return self._reparse_button

    def _populate(self) -> None:
        for failure in self._failures:
            row = self._table.rowCount()
            self._table.insertRow(row)
            values = (
                failure.get("error_class", ""),
                failure.get("subject") or "",
                failure.get("folder_display_name") or failure.get("folder_raw_name") or "",
                failure.get("uid", ""),
                failure.get("attempt_count", ""),
                failure.get("last_failed_at", ""),
            )
            for column, value in enumerate(values):
                self._table.setItem(row, column, QTableWidgetItem(str(value)))
        if not self._failures:
            self._table.insertRow(0)
            self._table.setItem(0, 0, QTableWidgetItem(strings.FAILURE_REVIEW_EMPTY))
        self._table.resizeColumnsToContents()

    def _selected_failure(self) -> Mapping[str, Any] | None:
        row = self._table.currentRow()
        if row < 0 or row >= len(self._failures):
            return None
        return self._failures[row]

    def _update_actions(self) -> None:
        failure = self._selected_failure()
        error_class = failure.get("error_class") if failure is not None else None
        self._fetch_button.setEnabled(error_class == "oversize")
        self._reparse_button.setEnabled(
            error_class == "parse"
            and failure is not None
            and type(failure.get("message_id")) is int
        )

    def _request_fetch(self) -> None:
        failure = self._selected_failure()
        if failure is not None and failure.get("error_class") == "oversize":
            self.fetch_requested.emit(failure)

    def _request_reparse(self) -> None:
        failure = self._selected_failure()
        if (
            failure is not None
            and failure.get("error_class") == "parse"
            and type(failure.get("message_id")) is int
        ):
            self.reparse_requested.emit(failure)
