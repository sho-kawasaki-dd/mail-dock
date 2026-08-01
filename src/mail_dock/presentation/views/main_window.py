"""Initial main-window shell used by the GUI bootstrap."""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QMainWindow

from mail_dock.presentation import strings
from mail_dock.presentation.context import AppContext


class MainWindow(QMainWindow):
    """Own the visible application window and presentation workers."""

    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self.context = context
        self.setWindowTitle(strings.MAIN_WINDOW_TITLE)
        self.setCentralWidget(QLabel(strings.APP_NAME))

    def start_startup_sync(self) -> None:
        """Hook for the sync worker added by the synchronization tasks."""

    def stop_workers(self) -> None:
        """Stop all GUI workers before the storage session closes."""

        self.context.stop_workers()
