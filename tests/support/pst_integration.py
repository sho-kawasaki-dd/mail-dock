"""Helpers for integration tests that exercise the bundled readpst process."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.importer import ImportOptions
from mail_dock.infrastructure.database.connection import connect
from mail_dock.infrastructure.database.message_repository import SqliteMessageRepository
from mail_dock.infrastructure.database.migrator import migrate
from mail_dock.infrastructure.database.pst_import_repository import SqlitePstImportRepository
from mail_dock.infrastructure.importers.readpst_importer import ReadPstImporter
from mail_dock.infrastructure.importers.readpst_locator import ReadPstLocator
from mail_dock.infrastructure.storage.eml_storage import EmlStorage
from mail_dock.infrastructure.storage.pst_import_storage import PstImportStorage
from mail_dock.infrastructure.storage.pst_manifest import PstManifestWriter
from mail_dock.infrastructure.storage.storage_root import initialize_root
from mail_dock.usecases.import_pst import run_stage_a, run_stage_b, snapshot_source_file


@dataclass
class PstImportRun:
    """The durable objects produced by one real readpst integration run."""

    root: Path
    source: Path
    database_path: Path
    connection: sqlite3.Connection
    import_uuid: str
    account_id: str
    import_id: int
    readpst_version: str

    def close(self) -> None:
        self.connection.close()


def bundled_readpst() -> tuple[ReadPstLocator, str]:
    """Return the validated bundled converter, or let the test skip it."""

    locator = ReadPstLocator()
    try:
        version = locator.get_version()
    except Exception as error:
        pytest.skip(f"bundled readpst is unavailable: {error}")
    return locator, version


def pst_fixture_path(variable: str, default_name: str) -> Path:
    """Resolve an opt-in PST fixture and skip when it is not installed."""

    configured = os.environ.get(variable)
    path = (
        Path(configured).expanduser()
        if configured
        else Path(__file__).resolve().parents[1] / "fixtures" / "pst" / default_name
    )
    if not path.is_file():
        pytest.skip(f"PST fixture is not installed: set {variable} to a .pst file")
    return path.resolve()


def import_real_pst(source: Path, root: Path, *, batch_size: int = 2) -> PstImportRun:
    """Run probe, Stage A, and resumable Stage B against a real PST file."""

    locator, readpst_version = bundled_readpst()
    initialize_root(root)
    database_path = root / "metadata.db"
    connection = connect(database_path)
    migrate(connection, database_path)
    source_snapshot = snapshot_source_file(source, pst_storage=PstImportStorage())
    import_uuid = str(uuid.uuid4())
    account_id = f"pst_{source_snapshot.source_sha256[:12]}_{import_uuid[:8]}"

    account_repository = SqliteMessageRepository(connection)
    account_repository.upsert_account(
        {
            "id": account_id,
            "provider_type": "pst_import",
            "display_name": "PST integration fixture",
            "username": source.name,
            "host": "",
            "port": 993,
            "is_enabled": 1,
        }
    )
    repository = SqlitePstImportRepository(connection)
    import_id = repository.create_import(
        {
            "import_uuid": import_uuid,
            "account_id": account_id,
            "source_filename": source.name,
            "source_sha256": source_snapshot.source_sha256,
            "source_size_bytes": source_snapshot.size_bytes,
            "source_mtime": source_snapshot.mtime_ns,
            "readpst_version": readpst_version,
            "options_json": json.dumps(
                {"charset": "cp932", "include_deleted": False}, ensure_ascii=False
            ),
            "status": "new",
            "is_active": 1,
        }
    )

    importer = ReadPstImporter(locator)
    importer.probe(source, cancel=CancelToken(), on_progress=lambda _count: None)
    pst_storage = PstImportStorage()
    options = ImportOptions("PST integration fixture", "cp932")
    with PstManifestWriter(root, import_uuid) as manifest:
        stage_a = run_stage_a(
            repository,
            manifest,
            importer,
            import_id=import_id,
            import_uuid=import_uuid,
            account_id=account_id,
            source=source,
            storage_root=root,
            source_snapshot=source_snapshot,
            readpst_version=readpst_version,
            options=options,
            cancel=CancelToken(),
            pst_storage=pst_storage,
        )
    with PstManifestWriter(root, import_uuid) as manifest:
        result = run_stage_b(
            repository,
            manifest,
            EmlStorage(root),
            import_id=import_id,
            import_uuid=import_uuid,
            account_id=account_id,
            staging_root=stage_a.staging_root,
            batch_size=batch_size,
            cancel=CancelToken(),
            pst_storage=pst_storage,
        )
    if result.status not in {"completed", "completed_with_errors"}:
        connection.close()
        raise AssertionError(f"real PST import did not complete: {result}")
    return PstImportRun(
        root,
        source,
        database_path,
        connection,
        import_uuid,
        account_id,
        import_id,
        readpst_version,
    )
