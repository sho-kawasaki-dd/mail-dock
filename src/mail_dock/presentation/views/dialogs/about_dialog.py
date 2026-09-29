"""Version, licensing, and runtime diagnostics dialog."""

from __future__ import annotations

import json

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from mail_dock import __version__
from mail_dock.infrastructure import app_paths
from mail_dock.infrastructure.diagnostics import run_self_check
from mail_dock.presentation.diagnostics_qt import qt_webengine_check

_SOURCE_URL = "https://github.com/sho-kawasaki-dd/mail-dock"


class AboutDialog(QDialog):
    """Show version and license information and run the read-only self-check."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("バージョン情報")
        self.resize(560, 460)

        layout = QVBoxLayout(self)
        self.version_label = QLabel(f"mail-dock {__version__}", self)
        layout.addWidget(self.version_label)
        layout.addWidget(
            QLabel(
                "GPL-3.0-or-later\nこのプログラムは無保証で提供されます。",
                self,
            )
        )

        resources = QHBoxLayout()
        self.license_button = QPushButton("LICENSE", self)
        self.third_party_button = QPushButton("THIRD-PARTY-LICENSES", self)
        self.bundle_licenses_button = QPushButton("同梱ライセンス資料", self)
        self.source_button = QPushButton("ソースリポジトリ", self)
        for button in (
            self.license_button,
            self.third_party_button,
            self.bundle_licenses_button,
            self.source_button,
        ):
            resources.addWidget(button)
        layout.addLayout(resources)

        self.diagnostics_button = QPushButton("実行環境を診断", self)
        layout.addWidget(self.diagnostics_button)
        self.results = QPlainTextEdit(self)
        self.results.setReadOnly(True)
        self.results.setPlaceholderText("診断結果")
        layout.addWidget(self.results, 1)

        self.license_button.clicked.connect(lambda: self._open_resource("LICENSE"))
        self.third_party_button.clicked.connect(
            lambda: self._open_resource("THIRD-PARTY-LICENSES.md")
        )
        self.bundle_licenses_button.clicked.connect(lambda: self._open_resource("licenses"))
        self.source_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(_SOURCE_URL)))
        self.diagnostics_button.clicked.connect(self._run_diagnostics)

    def _open_resource(self, relative_path: str) -> None:
        path = (app_paths.bundle_root() / relative_path).resolve()
        if not path.exists():
            QMessageBox.warning(self, "ファイルがありません", f"{relative_path} を開けません。")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _run_diagnostics(self) -> None:
        report = run_self_check(extra_checks=(qt_webengine_check(),))
        self.results.setPlainText(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
