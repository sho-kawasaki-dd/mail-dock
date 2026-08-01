"""Shared pytest fixtures for the mail-dock test suite."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from mail_dock.infrastructure.database.connection import connect
from mail_dock.infrastructure.storage.storage_root import initialize_root


@pytest.fixture
def tmp_storage_root(tmp_path: Path) -> Path:
    """Return a temporary storage root with a valid mail-dock marker."""

    root = tmp_path / "storage"
    initialize_root(root)
    return root


@pytest.fixture
def db_conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Return a writable connection to a real temporary SQLite file."""

    connection = connect(tmp_path / "metadata.db")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture(scope="session")
def qapp(qapp_args: list[str]) -> Iterator[Any]:
    """Create the one GUI application after private WebEngine schemes."""

    from PySide6.QtWidgets import QApplication

    from mail_dock.presentation.web.schemes import register_schemes

    register_schemes()
    application = QApplication.instance() or QApplication(qapp_args)
    yield application


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip Docker and GUI tests unless their opt-in environment is enabled."""

    del config
    skip_docker = pytest.mark.skip(reason="MAILDOCK_DOCKER=1 is required")
    skip_gui = pytest.mark.skip(reason="MAILDOCK_GUI=1 is required")
    for item in items:
        if "docker" in item.keywords and os.environ.get("MAILDOCK_DOCKER") != "1":
            item.add_marker(skip_docker)
        if "gui" in item.keywords and os.environ.get("MAILDOCK_GUI") != "1":
            item.add_marker(skip_gui)
