from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mail_dock.infrastructure.database.connection import connect
from mail_dock.infrastructure.database.reindex import rebuild_database
from mail_dock.infrastructure.storage.eml_storage import EmlStorage
from tests.support.pst_integration import bundled_readpst, import_real_pst, pst_fixture_path

pytestmark = pytest.mark.pst


@pytest.fixture
def pst_source() -> Path:
    bundled_readpst()
    return pst_fixture_path("MAILDOCK_PST_FIXTURE", "sample.pst")


def _pst_snapshot(
    connection: sqlite3.Connection, account_id: str
) -> dict[str, list[tuple[object, ...]]]:
    return {
        "accounts": connection.execute(
            "SELECT id, provider_type, display_name, username FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchall(),
        "folders": connection.execute(
            "SELECT raw_name, display_name, uidvalidity, last_seen_uid, is_sync_target "
            "FROM folders WHERE account_id = ? ORDER BY raw_name",
            (account_id,),
        ).fetchall(),
        "messages": connection.execute(
            "SELECT m.source_item_key, m.message_id, m.content_key, m.remote_state, "
            "m.local_state, m.relative_path, m.file_hash, m.subject, m.sender, "
            "m.recipient, m.date_sent, m.internal_date, m.size_bytes, m.has_attachment "
            "FROM messages AS m WHERE m.account_id = ? ORDER BY m.source_item_key",
            (account_id,),
        ).fetchall(),
        "contents": connection.execute(
            "SELECT m.source_item_key, c.subject_norm, c.sender_norm, c.body_text, "
            "c.attachment_names FROM message_contents AS c "
            "JOIN messages AS m ON m.id = c.message_id WHERE m.account_id = ? "
            "ORDER BY m.source_item_key",
            (account_id,),
        ).fetchall(),
    }


def test_reindex_rebuilds_pst_archive_from_eml_and_manifests(
    pst_source: Path, tmp_path: Path
) -> None:
    run = import_real_pst(pst_source, tmp_path / "storage")
    try:
        before = _pst_snapshot(run.connection, run.account_id)
        import_before = run.connection.execute(
            "SELECT import_uuid, source_sha256, status, is_active, total_files, "
            "ingested_count, failed_count FROM pst_imports WHERE id = ?",
            (run.import_id,),
        ).fetchone()
        items_before = run.connection.execute(
            "SELECT source_item_key, source_relative_path, folder_relative_path, "
            "source_size_bytes, source_sha256, final_relative_path, status, error_class "
            "FROM pst_import_items WHERE import_id = ? ORDER BY source_item_key",
            (run.import_id,),
        ).fetchall()
        assert import_before is not None
        run.connection.close()
        run.database_path.unlink()

        result = rebuild_database(
            run.database_path,
            EmlStorage(run.root),
            [],
        )
        assert result.account_count == 1
        assert result.message_count == len(before["messages"])
        assert result.folder_count == len(before["folders"])

        rebuilt = connect(run.database_path)
        try:
            assert _pst_snapshot(rebuilt, run.account_id) == before
            assert (
                rebuilt.execute(
                    "SELECT import_uuid, source_sha256, status, is_active, total_files, "
                    "ingested_count, failed_count FROM pst_imports"
                ).fetchone()
                == import_before
            )
            assert (
                rebuilt.execute(
                    "SELECT source_item_key, source_relative_path, folder_relative_path, "
                    "source_size_bytes, source_sha256, final_relative_path, status, error_class "
                    "FROM pst_import_items ORDER BY source_item_key"
                ).fetchall()
                == items_before
            )
            assert rebuilt.execute(
                "SELECT COUNT(*) FROM accounts WHERE provider_type = 'pst_import'"
            ).fetchone() == (1,)
            assert rebuilt.execute(
                "SELECT COUNT(*) FROM messages WHERE remote_state = 'no_remote' "
                "AND uid IS NULL AND uidvalidity IS NULL AND internal_date IS NULL"
            ).fetchone() == (len(before["messages"]),)
        finally:
            rebuilt.close()
    finally:
        if run.connection is not None:
            run.connection.close()
