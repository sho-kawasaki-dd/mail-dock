from __future__ import annotations

from pathlib import Path

import pytest

import mail_dock.__main__ as main
from mail_dock import config
from mail_dock.__main__ import StorageSession
from mail_dock.infrastructure.database.message_repository import SqliteMessageRepository
from mail_dock.infrastructure.database.search_repository import SqliteSearchRepository
from mail_dock.infrastructure.storage.eml_storage import EmlStorage
from mail_dock.infrastructure.storage.manifest import ManifestWriter
from mail_dock.presentation.context import AppContext


def test_app_context_borrows_session_resources_and_builds_thread_local_factories(
    tmp_storage_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = config.AppConfig()
    monkeypatch.setattr(main, "check_free_space", lambda path: None)
    renderer = object()
    saved: list[config.AppConfig] = []
    monkeypatch.setattr(config, "save", saved.append)

    with StorageSession(settings, tmp_storage_root) as session:
        assert session.capabilities is not None
        assert session.capability_level is not None
        context = AppContext(session, settings, renderer_factory=lambda: renderer)

        assert context.storage_root == session.root
        assert context.storage_lock is session.storage_lock
        assert context.connection_manager is session.connection_manager
        assert context.capabilities == session.capabilities.as_dict()
        assert context.capability_level == session.capability_level.value
        assert context.encryption_declaration == session.encryption_declaration
        assert context.credential_storage == session.credential_storage_mode
        assert context.credential_store is session.active_credential_store
        assert isinstance(context.create_message_repository(), SqliteMessageRepository)
        assert isinstance(context.create_search_repository(), SqliteSearchRepository)
        assert isinstance(context.create_eml_storage(), EmlStorage)
        manifest = context.create_manifest_writer("account-1")
        assert isinstance(manifest, ManifestWriter)
        manifest.close()
        assert context.create_message_renderer() is renderer

        updated = config.AppConfig(sync_on_startup=False)
        context.save_settings(updated)
        assert context.settings is updated
        assert session.settings is updated
        assert saved[-1] is updated


def test_app_context_does_not_close_borrowed_session_resources(
    tmp_storage_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = config.AppConfig()
    monkeypatch.setattr(main, "check_free_space", lambda path: None)

    with StorageSession(settings, tmp_storage_root) as session:
        context = AppContext(session, settings)
        context.stop_workers()
        assert session.connection_manager.get_connection().execute("SELECT 1").fetchone() == (1,)
        assert session.storage_lock.held
