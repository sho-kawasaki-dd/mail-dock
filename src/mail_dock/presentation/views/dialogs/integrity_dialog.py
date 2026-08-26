"""Integrity verification and metadata-repair dialog."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from mail_dock.presentation import strings
from mail_dock.presentation.errors import user_message
from mail_dock.presentation.threads.verify_worker import VerifyErrorNotification, VerifyWorker
from mail_dock.presentation.views.dialogs.confirmation_dialog import ConfirmationDialog
from mail_dock.usecases.reindex import ReindexProgress, ReindexResult
from mail_dock.usecases.reparse import ReparseResult
from mail_dock.usecases.verify import (
    FullVerifyResult,
    ManifestVerifyResult,
    OrphanScanResult,
    QuickVerifyResult,
    RangeVerifyResult,
    VerificationIssue,
    VerifyProgress,
)


class _VerifyWorker(Protocol):
    verify_progress: Any
    verify_result: Any
    error_reported: Any
    cancelled: Any

    def quick_verify(self) -> object: ...

    def range_verify(self) -> object: ...

    def full_verify(self) -> object: ...

    def orphan_scan(self) -> object: ...

    def verify_manifest(self) -> object: ...

    def reindex(self) -> object: ...

    def reparse(self, *, only_failed: bool = True) -> object: ...

    def cancel_all(self) -> None: ...


Mode = str


class IntegrityDialog(QDialog):
    """Run one integrity operation and present its bounded result list."""

    operation_requested = Signal(str)
    operation_finished = Signal()

    _MODES: tuple[tuple[str, Mode], ...] = (
        (strings.INTEGRITY_MODE_QUICK, "quick"),
        (strings.INTEGRITY_MODE_RANGE, "range"),
        (strings.INTEGRITY_MODE_FULL, "full"),
        (strings.INTEGRITY_MODE_ORPHANS, "orphans"),
        (strings.INTEGRITY_MODE_MANIFEST, "manifest"),
        (strings.INTEGRITY_MODE_REINDEX, "reindex"),
        (strings.INTEGRITY_MODE_REPARSE, "reparse"),
    )

    def __init__(
        self,
        worker: VerifyWorker | _VerifyWorker,
        *,
        parent: Any = None,
        confirmation: Callable[[str, Any], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self._worker = worker
        self._confirmation = confirmation or self._confirm
        self._token: object | None = None
        self._running = False
        self._build_ui()
        self._connect_worker()

    @property
    def mode_selector(self) -> QComboBox:
        return self._mode_selector

    @property
    def run_button(self) -> QPushButton:
        return self._run_button

    @property
    def cancel_button(self) -> QPushButton:
        return self._cancel_button

    @property
    def progress_bar(self) -> QProgressBar:
        return self._progress_bar

    @property
    def results_list(self) -> QListWidget:
        return self._results_list

    @property
    def status_label(self) -> QLabel:
        return self._status_label

    def start_mode(self, mode: Mode) -> bool:
        """Start ``mode`` and return whether a worker task was queued."""

        if self._running:
            return False
        index = self._mode_selector.findData(mode)
        if index < 0:
            return False
        self._mode_selector.setCurrentIndex(index)
        if mode == "reindex" and not self._confirmation(strings.INTEGRITY_CONFIRM_REINDEX, self):
            return False
        try:
            if mode == "quick":
                method = self._worker.quick_verify
            elif mode == "range":
                method = self._worker.range_verify
            elif mode == "full":
                method = self._worker.full_verify
            elif mode == "orphans":
                method = self._worker.orphan_scan
            elif mode == "manifest":
                method = self._worker.verify_manifest
            elif mode == "reindex":
                method = self._worker.reindex
            elif mode == "reparse":
                method = self._worker.reparse
            else:
                raise RuntimeError(f"unsupported integrity mode: {mode}")
            self._token = method()
        except Exception as error:
            self._show_error(error)
            return False
        self._running = True
        self._run_button.setEnabled(False)
        self._mode_selector.setEnabled(False)
        self._cancel_button.setEnabled(True)
        self._progress_bar.setRange(0, 0)
        self._results_list.clear()
        self._status_label.setText(strings.INTEGRITY_STATUS_RUNNING)
        self.operation_requested.emit(mode)
        return True

    def accept(self) -> None:
        if self._running:
            return
        super().accept()

    def reject(self) -> None:
        if self._running:
            self._worker.cancel_all()
            return
        super().reject()

    def _build_ui(self) -> None:
        self.setWindowTitle(strings.INTEGRITY_TITLE)
        self.resize(640, 440)
        layout = QVBoxLayout(self)
        self._mode_selector = QComboBox(self)
        for label, mode in self._MODES:
            self._mode_selector.addItem(label, mode)
        layout.addWidget(self._mode_selector)

        self._status_label = QLabel(strings.INTEGRITY_STATUS_READY, self)
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)
        self._progress_bar = QProgressBar(self)
        self._progress_bar.setRange(0, 1)
        self._progress_bar.setValue(0)
        layout.addWidget(self._progress_bar)
        self._results_list = QListWidget(self)
        self._results_list.setObjectName("integrityResultsList")
        layout.addWidget(self._results_list, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=self)
        self._run_button = QPushButton(strings.INTEGRITY_RUN, self)
        self._cancel_button = QPushButton(strings.SEARCH_CANCEL, self)
        self._cancel_button.setEnabled(False)
        buttons.addButton(self._run_button, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(self._cancel_button, QDialogButtonBox.ButtonRole.RejectRole)
        self._close_button = buttons.button(QDialogButtonBox.StandardButton.Close)
        layout.addWidget(buttons)
        self._run_button.clicked.connect(self._start_selected_mode)
        self._cancel_button.clicked.connect(self._cancel)
        if self._close_button is not None:
            self._close_button.clicked.connect(self.reject)

    def _connect_worker(self) -> None:
        self._worker.verify_progress.connect(self._show_progress)
        self._worker.verify_result.connect(self._show_result)
        self._worker.error_reported.connect(self._show_worker_error)
        self._worker.cancelled.connect(self._cancelled)

    def _start_selected_mode(self) -> None:
        mode = self._mode_selector.currentData()
        if isinstance(mode, str):
            self.start_mode(mode)

    def _cancel(self) -> None:
        if self._running:
            self._worker.cancel_all()
            self._status_label.setText(strings.INTEGRITY_STATUS_CANCELLING)
            self._cancel_button.setEnabled(False)

    def _show_progress(self, progress: object) -> None:
        if not isinstance(progress, (VerifyProgress, ReindexProgress)):
            return
        checked = (
            progress.checked_count
            if isinstance(progress, VerifyProgress)
            else progress.processed_count
        )
        total = progress.total_count
        self._progress_bar.setRange(0, max(total, 1))
        self._progress_bar.setValue(min(checked, max(total, 1)))
        self._status_label.setText(
            strings.INTEGRITY_STATUS_PROGRESS.format(
                checked=checked,
                total=total,
                path=progress.current_path
                if isinstance(progress, VerifyProgress)
                else progress.relative_path,
            )
        )

    def _show_result(self, result: object) -> None:
        self._finish()
        self._results_list.addItems(_result_lines(result))
        self._status_label.setText(_result_summary(result))

    def _show_worker_error(self, notification: object) -> None:
        self._finish()
        if isinstance(notification, VerifyErrorNotification):
            self._show_error_text(notification.message)
        elif isinstance(notification, BaseException):
            self._show_error(notification)
        else:
            self._show_error_text(strings.ERROR_UNKNOWN)

    def _cancelled(self) -> None:
        self._finish()
        self._status_label.setText(strings.INTEGRITY_STATUS_CANCELLED)

    def _finish(self) -> None:
        self._running = False
        self._token = None
        self._run_button.setEnabled(True)
        self._mode_selector.setEnabled(True)
        self._cancel_button.setEnabled(False)
        self._progress_bar.setRange(0, 1)
        self._progress_bar.setValue(1)
        self.operation_finished.emit()

    def _show_error(self, error: BaseException) -> None:
        self._finish()
        self._show_error_text(user_message(error))

    def _show_error_text(self, message: str) -> None:
        self._status_label.setText(message)
        self._results_list.addItem(message)

    @staticmethod
    def _confirm(message: str, parent: Any) -> bool:
        return ConfirmationDialog(message, parent).confirmed()


def _result_summary(result: object) -> str:
    if isinstance(result, QuickVerifyResult):
        return strings.INTEGRITY_RESULT_QUICK.format(
            checked=result.checked_count,
            issues=result.mismatch_count + result.missing_count,
        )
    if isinstance(result, RangeVerifyResult):
        return strings.INTEGRITY_RESULT_RANGE.format(
            checked=result.checked_count,
            issues=len(result.issues),
            repaired=result.repaired_count,
        )
    if isinstance(result, FullVerifyResult):
        return strings.INTEGRITY_RESULT_FULL.format(
            checked=result.checked_count,
            issues=len(result.issues),
        )
    if isinstance(result, OrphanScanResult):
        return strings.INTEGRITY_RESULT_ORPHANS.format(
            checked=result.checked_count,
            registerable=len(result.registerable),
            quarantined=len(result.quarantined_paths),
        )
    if isinstance(result, ManifestVerifyResult):
        return strings.INTEGRITY_RESULT_MANIFEST.format(
            files=result.files_checked,
            records=result.records_checked,
            repaired=result.repaired_bytes,
        )
    if isinstance(result, ReindexResult):
        return strings.INTEGRITY_RESULT_REINDEX.format(
            accounts=result.account_count,
            folders=result.folder_count,
            messages=result.message_count,
            skipped=result.skipped_count,
        )
    if isinstance(result, ReparseResult):
        return strings.INTEGRITY_RESULT_REPARSE.format(
            reparsed=result.reparsed_count,
            skipped=result.skipped_count,
        )
    return strings.INTEGRITY_STATUS_COMPLETE


def _result_lines(result: object) -> list[str]:
    if isinstance(result, QuickVerifyResult):
        return [f"{strings.INTEGRITY_MISSING}: {path}" for path in result.missing_paths] + [
            f"{strings.INTEGRITY_SIZE_MISMATCH}: {path}" for path in result.size_mismatch_paths
        ]
    if isinstance(result, (RangeVerifyResult, FullVerifyResult)):
        return [_issue_line(issue) for issue in result.issues]
    if isinstance(result, OrphanScanResult):
        return [
            f"{strings.INTEGRITY_ORPHAN_REGISTERABLE}: {candidate.relative_path}"
            for candidate in result.registerable
        ] + [f"{strings.INTEGRITY_ORPHAN_QUARANTINED}: {path}" for path in result.quarantined_paths]
    if isinstance(result, ManifestVerifyResult):
        if result.damaged_paths:
            return [
                f"{strings.INTEGRITY_MANIFEST_DAMAGED}: {path}" for path in result.damaged_paths
            ]
        if result.repaired_bytes:
            return [strings.INTEGRITY_MANIFEST_REPAIRED.format(bytes=result.repaired_bytes)]
    if isinstance(result, ReindexResult):
        return [f"{strings.INTEGRITY_REINDEX_WARNING}: {warning}" for warning in result.warnings]
    return []


def _issue_line(issue: VerificationIssue) -> str:
    return f"{issue.reason}: {issue.relative_path}"


__all__ = ["IntegrityDialog"]
