"""Initial storage-root selection wizard."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QLineEdit,
    QPushButton,
    QWizard,
    QWizardPage,
)

from mail_dock.infrastructure.storage.storage_root import initialize_root
from mail_dock.presentation import strings


class SetupWizard(QWizard):
    """Collect the storage root before a ``StorageSession`` is created."""

    def __init__(self, initial_root: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle(strings.WIZARD_TITLE)
        self._selected_root: Path | None = None
        self._root_edit = QLineEdit(str(initial_root) if initial_root else "")

        page = QWizardPage()
        page.setTitle(strings.WIZARD_PAGE_STORAGE_TITLE)
        layout = QFormLayout(page)
        layout.addRow(strings.WIZARD_LABEL_STORAGE_ROOT, self._root_edit)
        browse_button = QPushButton(strings.WIZARD_BUTTON_BROWSE, page)
        browse_button.clicked.connect(self._browse)
        layout.addRow(browse_button)
        self.addPage(page)

    @property
    def selected_root(self) -> Path | None:
        """Return the initialized root after the wizard is accepted."""

        return self._selected_root

    def accept(self) -> None:
        value = self._root_edit.text().strip()
        if not value:
            self._root_edit.setFocus()
            return
        self._selected_root = Path(value).expanduser()
        initialize_root(self._selected_root)
        super().accept()

    def _browse(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, strings.WIZARD_DIALOG_SELECT_ROOT)
        if selected:
            self._root_edit.setText(selected)
