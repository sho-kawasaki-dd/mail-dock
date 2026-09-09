"""PST import wizard and its non-blocking probe stage."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, cast

from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWizard,
    QWizardPage,
)

from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.importer import ArchiveInfo
from mail_dock.presentation import strings
from mail_dock.presentation.errors import user_message
from mail_dock.presentation.threads.sync_worker import (
    PstImportDecisionRequest,
    PstImportProgress,
    PstImportResult,
    SyncErrorNotification,
    SyncWorker,
)
from mail_dock.presentation.threads.worker import Worker
from mail_dock.usecases.import_pst import ImportJobDecision


class _SourcePage(QWizardPage):
    def __init__(self, complete: Any) -> None:
        super().__init__()
        self._complete = complete

    def isComplete(self) -> bool:  # noqa: N802
        return bool(self._complete())


class ImportWizard(QWizard):
    """Collect PST options and hand the durable import to ``SyncWorker``."""

    def __init__(self, context: Any, sync_worker: SyncWorker, parent: Any = None) -> None:
        super().__init__(parent)
        self._context = context
        self._sync_worker = sync_worker
        self._probe_worker: Worker | None = None
        self._probe_token: CancelToken | None = None
        self._import_token: CancelToken | None = None
        self._info: ArchiveInfo | None = None
        self._import_started = False
        self.setWindowTitle(strings.PST_WIZARD_TITLE)
        self.setOption(QWizard.WizardOption.NoCancelButtonOnLastPage, True)
        self._build_source_page()
        self._build_options_page()
        self._build_progress_page()
        self._build_summary_page()
        sync_worker.progress.connect(self._show_progress)
        sync_worker.pst_import_result.connect(self._show_result)
        sync_worker.pst_import_decision_required.connect(self._choose_existing_job_action)
        sync_worker.pst_import_cancelled.connect(self._show_cancelled)
        sync_worker.error_reported.connect(self._show_error)

    def _build_source_page(self) -> None:
        self._source_edit = QLineEdit()
        self._source_page = _SourcePage(
            lambda: bool(self._source_edit.text().strip()) and self._info is not None
        )
        self._source_page.setTitle(strings.PST_WIZARD_SOURCE_TITLE)
        layout = QFormLayout(self._source_page)
        self._source_edit.textChanged.connect(self._source_changed)
        browse = QPushButton(strings.PST_WIZARD_BROWSE, self._source_page)
        browse.clicked.connect(self._browse)
        row = QVBoxLayout()
        row.addWidget(self._source_edit)
        row.addWidget(browse)
        layout.addRow(strings.PST_WIZARD_SOURCE_LABEL, row)
        self._probe_button = QPushButton(strings.PST_WIZARD_PROBE, self._source_page)
        self._probe_button.clicked.connect(self._probe)
        layout.addRow(self._probe_button)
        self._probe_status = QLabel()
        self._probe_status.setWordWrap(True)
        layout.addRow(self._probe_status)
        self.addPage(self._source_page)

    def _build_options_page(self) -> None:
        self._options_page = QWizardPage()
        self._options_page.setTitle(strings.PST_WIZARD_OPTIONS_TITLE)
        layout = QFormLayout(self._options_page)
        self._display_name = QLineEdit()
        self._charset = QLineEdit("cp932")
        self._include_deleted = QCheckBox(strings.PST_WIZARD_INCLUDE_DELETED)
        layout.addRow(strings.PST_WIZARD_DISPLAY_NAME, self._display_name)
        layout.addRow(strings.PST_WIZARD_CHARSET, self._charset)
        layout.addRow(self._include_deleted)
        self.addPage(self._options_page)

    def _build_progress_page(self) -> None:
        self._progress_page = QWizardPage()
        self._progress_page.setTitle(strings.PST_WIZARD_PROGRESS_TITLE)
        layout = QVBoxLayout(self._progress_page)
        self._progress_label = QLabel()
        self._progress_label.setWordWrap(True)
        layout.addWidget(self._progress_label)
        self._progress_page_id = self.addPage(self._progress_page)

    def _build_summary_page(self) -> None:
        self._summary_page = QWizardPage()
        self._summary_page.setTitle(strings.PST_WIZARD_SUMMARY_TITLE)
        layout = QVBoxLayout(self._summary_page)
        self._summary_label = QLabel()
        self._summary_label.setWordWrap(True)
        layout.addWidget(self._summary_label)
        self._summary_page_id = self.addPage(self._summary_page)

    def _browse(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self, strings.PST_WIZARD_SOURCE_TITLE, "", "Outlook PST (*.pst);;All files (*)"
        )
        if selected:
            self._source_edit.setText(selected)

    def _source_changed(self, value: str) -> None:
        self._info = None
        self._probe_status.clear()
        self._probe_button.setEnabled(bool(value.strip()))
        self._source_page.completeChanged.emit()

    def _probe(self) -> None:
        source = Path(self._source_edit.text().strip())
        if not source.is_file():
            self._probe_status.setText("PSTファイルを選択してください。")
            return
        importer_factory = getattr(self._context, "create_pst_importer", None)
        if not callable(importer_factory):
            self._probe_status.setText("PST変換機能が利用できません。")
            return
        self._probe_button.setEnabled(False)
        self._probe_status.setText(strings.PST_WIZARD_PROBE_RUNNING)
        self._probe_worker = Worker()
        self._probe_worker.result.connect(self._probe_finished)
        self._probe_worker.failed.connect(self._probe_failed)
        self._probe_worker.start()
        importer = importer_factory()
        probe_token = CancelToken()
        self._probe_token = probe_token
        self._probe_worker.submit(
            lambda: importer.probe(source, cancel=probe_token, on_progress=lambda _count: None),
            probe_token,
        )

    def _probe_finished(self, value: object) -> None:
        if not isinstance(value, ArchiveInfo):
            return
        self._info = value
        self._display_name.setText(Path(self._source_edit.text()).stem)
        self._probe_status.setText(strings.PST_WIZARD_PROBE_RESULT.format(count=len(value.folders)))
        self._probe_button.setEnabled(True)
        self._source_page.completeChanged.emit()
        self._probe_worker_stop()

    def _probe_failed(self, error: object) -> None:
        self._probe_status.setText(user_message(cast(BaseException, error)))
        self._probe_button.setEnabled(True)
        self._probe_worker_stop()

    def _probe_worker_stop(self) -> None:
        if self._probe_worker is not None:
            self._probe_worker.stop()
            self._probe_worker = None
        self._probe_token = None

    def initializePage(self, page_id: int) -> None:  # noqa: N802
        super().initializePage(page_id)
        if page_id == self._progress_page_id and not self._import_started:
            self._start_import()

    def _start_import(self, decision: ImportJobDecision | None = None) -> None:
        source = Path(self._source_edit.text().strip()).expanduser().resolve()
        storage_root = getattr(self._context, "storage_root", None)
        if isinstance(storage_root, Path):
            required = int(source.stat().st_size * 2.5 + 0.5)
            if shutil.disk_usage(storage_root).free < required:
                self._summary_label.setText(strings.PST_WIZARD_CAPACITY)
                self.next()
                return
        self._import_started = True
        self._progress_label.setText(strings.PST_WIZARD_PROBE_RUNNING)
        self._import_token = self._sync_worker.import_pst(
            source,
            display_name=self._display_name.text().strip() or source.stem,
            charset=self._charset.text().strip() or "cp932",
            include_deleted=self._include_deleted.isChecked(),
            decision=decision,
        )

    def _choose_existing_job_action(self, value: object) -> None:
        if not isinstance(value, PstImportDecisionRequest):
            return
        if value.has_incomplete:
            dialog = QMessageBox(self)
            dialog.setWindowTitle(strings.PST_WIZARD_TITLE)
            dialog.setText("同じPSTの未完了ジョブが見つかりました。")
            resume = dialog.addButton("再開", QMessageBox.ButtonRole.AcceptRole)
            discard = dialog.addButton("破棄してやり直す", QMessageBox.ButtonRole.DestructiveRole)
            cancel = dialog.addButton(QMessageBox.StandardButton.Cancel)
            dialog.exec()
            clicked = dialog.clickedButton()
            if clicked is resume:
                decision = ImportJobDecision.RESUME
            elif clicked is discard:
                decision = ImportJobDecision.DISCARD_INCOMPLETE
            elif clicked is cancel:
                decision = ImportJobDecision.CANCEL
            else:
                return
        elif value.has_completed:
            dialog = QMessageBox(self)
            dialog.setWindowTitle(strings.PST_WIZARD_TITLE)
            dialog.setText("同じPSTの完成済みアーカイブがあります。")
            reimport = dialog.addButton("再変換", QMessageBox.ButtonRole.AcceptRole)
            cancel = dialog.addButton(QMessageBox.StandardButton.Cancel)
            dialog.exec()
            decision = (
                ImportJobDecision.REIMPORT
                if dialog.clickedButton() is reimport
                else ImportJobDecision.CANCEL
            )
        else:
            return
        self._import_started = False
        self._start_import(decision)

    def _show_progress(self, value: object) -> None:
        if isinstance(value, PstImportProgress):
            self._progress_label.setText(
                strings.PST_WIZARD_PROGRESS.format(stage=value.stage, count=value.processed_count)
            )

    def _show_result(self, value: object) -> None:
        if not isinstance(value, PstImportResult):
            return
        self._import_token = None
        self._summary_label.setText(
            strings.PST_WIZARD_SUMMARY.format(
                imported=value.imported_count,
                failed=value.failed_count,
                remaining=value.skipped_count,
            )
            + "\n"
            + strings.PST_WIZARD_KEEP_SOURCE
        )
        self.next()

    def _show_cancelled(self, _value: object) -> None:
        self._import_token = None
        self._summary_label.setText("PSTの取り込みをキャンセルしました。未完了の処理は再開できます。")
        self.next()

    def _show_error(self, value: object) -> None:
        if not isinstance(value, SyncErrorNotification) or value.operation != "pst_import":
            return
        self._import_token = None
        self._summary_label.setText(value.message + "\n" + strings.PST_WIZARD_KEEP_SOURCE)
        self.next()

    def reject(self) -> None:
        if self._probe_token is not None:
            self._probe_token.cancel()
        if self._import_token is not None:
            self._import_token.cancel()
            self._import_token = None
        self._probe_worker_stop()
        super().reject()


__all__ = ["ImportWizard"]
