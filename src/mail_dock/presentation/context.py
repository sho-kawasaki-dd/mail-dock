"""GUI composition context.

Views and view models receive this object rather than importing infrastructure
implementations directly. The resource owners remain in ``StorageSession``;
this context only exposes their active lifetime to the presentation layer.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from mail_dock import config

if TYPE_CHECKING:
    from mail_dock.__main__ import StorageSession


class AppContext:
    """Objects shared by the GUI after a storage session has started."""

    def __init__(self, session: StorageSession, settings: config.AppConfig) -> None:
        self.storage_root = session.root
        self.settings = settings
        self.connection_manager = session.connection_manager
        self._session = session

    @property
    def database_path(self) -> Path:
        """Return the active metadata database path."""

        return self.storage_root / "metadata.db"

    def stop_workers(self) -> None:
        """Stop presentation workers before the owning session is released."""

    def save_settings(self, settings: config.AppConfig) -> None:
        """Persist settings changes through the configuration module."""

        config.save(settings)
        self.settings = settings

    def build_main_window(self) -> Any:
        """Construct the main window through the presentation composition root."""

        from mail_dock.presentation.views.main_window import MainWindow

        return MainWindow(self)
