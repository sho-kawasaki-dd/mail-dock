"""Shared pytest fixtures for the mail-dock test suite."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

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


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip Docker tests unless the caller explicitly enables the environment."""

    del config
    if os.environ.get("MAILDOCK_DOCKER") == "1":
        return
    skip_docker = pytest.mark.skip(reason="MAILDOCK_DOCKER=1 is required")
    for item in items:
        if "docker" in item.keywords:
            item.add_marker(skip_docker)
