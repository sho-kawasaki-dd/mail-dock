"""Confirmation dialogs for actions with external or destructive effects."""

from __future__ import annotations

from PySide6.QtWidgets import QMessageBox, QWidget

from mail_dock.presentation import strings


class ConfirmationDialog(QMessageBox):
    """A standard Yes/No confirmation with a conservative No default."""

    def __init__(self, message: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setIcon(QMessageBox.Icon.Warning)
        self.setWindowTitle(strings.DIALOG_CONFIRM_TITLE)
        self.setText(message)
        self.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        self.setDefaultButton(QMessageBox.StandardButton.No)

    def confirmed(self) -> bool:
        """Execute the dialog and return whether the user selected Yes."""

        return self.exec() == QMessageBox.StandardButton.Yes

    @classmethod
    def external_link(cls, url: str, parent: QWidget | None = None) -> bool:
        return cls(strings.DIALOG_CONFIRM_EXTERNAL_LINK.format(url=url), parent).confirmed()

    @classmethod
    def save_executable(cls, filename: str, parent: QWidget | None = None) -> bool:
        return cls(
            strings.DIALOG_CONFIRM_SAVE_EXECUTABLE.format(filename=filename),
            parent,
        ).confirmed()

    @classmethod
    def overwrite(cls, filename: str, parent: QWidget | None = None) -> bool:
        return cls(strings.DIALOG_CONFIRM_OVERWRITE.format(filename=filename), parent).confirmed()


def confirm_external_link(url: str, parent: QWidget | None = None) -> bool:
    return ConfirmationDialog.external_link(url, parent)


def confirm_save_executable(filename: str, parent: QWidget | None = None) -> bool:
    return ConfirmationDialog.save_executable(filename, parent)


def confirm_overwrite(filename: str, parent: QWidget | None = None) -> bool:
    return ConfirmationDialog.overwrite(filename, parent)
