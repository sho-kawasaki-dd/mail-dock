from __future__ import annotations

import os
from pathlib import Path

import pytest

from mail_dock.domain.errors import ConverterFailed
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.importer import ImportOptions
from mail_dock.infrastructure.database.pst_import_repository import SqlitePstImportRepository
from mail_dock.infrastructure.importers.readpst_importer import ReadPstImporter
from mail_dock.infrastructure.storage.eml_storage import EmlStorage
from mail_dock.infrastructure.storage.pst_manifest import PstManifestReader, PstManifestWriter
from mail_dock.usecases.import_pst import verify_generation
from tests.support.pst_integration import bundled_readpst, import_real_pst, pst_fixture_path

pytestmark = pytest.mark.pst


@pytest.fixture
def pst_source() -> Path:
    bundled_readpst()
    return pst_fixture_path("MAILDOCK_PST_FIXTURE", "sample.pst")


def test_real_readpst_import_preserves_mail_data_and_confines_paths(
    pst_source: Path, tmp_path: Path
) -> None:
    run = import_real_pst(pst_source, tmp_path / "storage")
    try:
        connection = run.connection
        status = connection.execute(
            "SELECT status, is_active, total_files, ingested_count, failed_count "
            "FROM pst_imports WHERE id = ?",
            (run.import_id,),
        ).fetchone()
        assert status is not None
        assert status[0] in {"completed", "completed_with_errors"}
        assert status[1] == 1
        assert status[2] > 0
        assert status[3] == status[2]

        messages = connection.execute(
            "SELECT m.source_item_key, m.subject, m.remote_state, m.uid, "
            "m.uidvalidity, m.internal_date, m.has_attachment, m.relative_path, "
            "f.raw_name FROM messages AS m JOIN folders AS f ON f.id = m.folder_id "
            "WHERE m.account_id = ? ORDER BY m.source_item_key",
            (run.account_id,),
        ).fetchall()
        assert messages
        assert all(row[2:6] == ("no_remote", None, None, None) for row in messages)
        assert all(row[8] != "." for row in messages)
        assert any(row[6] for row in messages), "fixture must contain an attachment"
        assert any(any(ord(character) > 127 for character in (row[1] or "")) for row in messages), (
            "fixture must contain Japanese or other non-ASCII message text"
        )

        folders = connection.execute(
            "SELECT raw_name, display_name, uidvalidity, last_seen_uid, is_sync_target "
            "FROM folders WHERE account_id = ?",
            (run.account_id,),
        ).fetchall()
        assert folders
        assert all(row[2:] == (None, 0, 0) for row in folders)
        assert max(row[0].count("/") for row in folders) >= 2, (
            "fixture must contain a nested folder hierarchy"
        )
        assert any(any(ord(character) > 127 for character in row[0]) for row in folders)

        item_rows = connection.execute(
            "SELECT source_item_key, source_relative_path, folder_relative_path, "
            "final_relative_path, status FROM pst_import_items WHERE import_id = ?",
            (run.import_id,),
        ).fetchall()
        stage_root = run.root / "tmp" / "pstimp" / run.import_uuid[:8]
        assert not stage_root.exists()
        for source_key, source_relative, folder_relative, final_relative, item_status in item_rows:
            assert source_key == source_relative
            assert item_status == "saved"
            assert not Path(source_relative).is_absolute()
            assert ".." not in Path(source_relative).parts
            assert not Path(folder_relative).is_absolute()
            assert ".." not in Path(folder_relative).parts
            final_path = (run.root / final_relative).resolve()
            final_path.relative_to(run.root.resolve())
            assert final_path.is_file()

        manifest = PstManifestReader(run.root, run.import_uuid)
        events = list(manifest.read_all_events())
        assert any(event["event"] == "import_ready" for event in events)
        assert sum(event["event"] == "item_saved" for event in events) == len(item_rows)
        with PstManifestWriter(run.root, run.import_uuid) as manifest_writer:
            verification = verify_generation(
                SqlitePstImportRepository(connection),
                manifest_writer,
                EmlStorage(run.root),
                import_id=run.import_id,
                import_uuid=run.import_uuid,
            )
        assert verification.item_count == len(item_rows)
    finally:
        run.close()


def test_real_readpst_rejects_corrupt_archive(pst_source: Path, tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.pst"
    source_bytes = pst_source.read_bytes()
    corrupt.write_bytes(source_bytes[: max(32, len(source_bytes) // 2)])
    importer = ReadPstImporter()
    with pytest.raises(ConverterFailed, match="readpst failed"):
        importer.extract(
            corrupt,
            tmp_path / "staging",
            ImportOptions("Corrupt PST", "cp932"),
            cancel=CancelToken(),
            on_progress=lambda _count: None,
        )


def test_hostile_folder_fixture_never_escapes_staging(tmp_path: Path) -> None:
    hostile = os.environ.get("MAILDOCK_PST_HOSTILE")
    if not hostile:
        pytest.skip("set MAILDOCK_PST_HOSTILE to the Outlook-generated hostile PST fixture")
    source = Path(hostile).expanduser().resolve()
    if not source.is_file():
        pytest.skip(f"PST fixture is not installed: {source}")
    bundled_readpst()
    staging = tmp_path / "staging"
    importer = ReadPstImporter()
    try:
        importer.extract(
            source,
            staging,
            ImportOptions("Hostile PST", "cp932"),
            cancel=CancelToken(),
            on_progress=lambda _count: None,
        )
    except ConverterFailed as error:
        assert "cannot be created on this Windows filesystem" in str(error)
    for path in staging.rglob("*") if staging.exists() else ():
        path.resolve().relative_to(staging.resolve())
