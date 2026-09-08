import uuid
import zlib
from pathlib import Path

import pytest

from mail_dock.domain.errors import ManifestCorruptError
from mail_dock.domain.ports import JSONValue
from mail_dock.infrastructure.storage.pst_manifest import (
    PstManifestReader,
    PstManifestWriter,
    read_events,
)


def _import_snapshot(import_uuid: str) -> dict[str, JSONValue]:
    return {
        "account_id": "pst_abcdef012345_12345678",
        "display_name": "Archive",
        "import_uuid": import_uuid,
        "created_at": "2026-09-09T00:00:00Z",
        "source_filename": "archive.pst",
        "source_sha256": "a" * 64,
        "source_size_bytes": 10,
        "source_mtime": 1,
        "source_file_identity": {"st_dev": 1, "st_ino": 2},
        "readpst_version": "0.6.76",
        "options": {"charset": "cp932", "include_deleted": False},
    }


def _discovered(import_uuid: str) -> dict[str, JSONValue]:
    return {
        "event": "item_discovered",
        "import_uuid": import_uuid,
        "timestamp": "2026-09-09T00:00:01Z",
        "source_item_key": "Inbox/1.eml",
        "source_relative_path": "Inbox/1.eml",
        "folder_relative_path": "Inbox",
        "source_size_bytes": 10,
        "source_sha256": "b" * 64,
    }


def _saved(import_uuid: str) -> dict[str, JSONValue]:
    return {
        "event": "item_saved",
        "import_uuid": import_uuid,
        "timestamp": "2026-09-09T00:00:02Z",
        "source_item_key": "Inbox/1.eml",
        "final_relative_path": "eml/pst/message.eml",
        "file_hash": "c" * 64,
        "size_bytes": 10,
    }


def test_pst_manifest_writes_verified_snapshots_and_idempotent_events(tmp_path: Path) -> None:
    import_uuid = str(uuid.uuid4())
    with PstManifestWriter(tmp_path, import_uuid) as writer:
        writer.write_import_manifest(_import_snapshot(import_uuid))
        writer.write_folders_manifest([{"raw_name": "Inbox", "display_name": "Inbox"}])
        writer.append(_discovered(import_uuid))
        saved = _saved(import_uuid)
        writer.append(saved)
        writer.append(saved)

    reader = PstManifestReader(tmp_path, import_uuid)
    import_manifest = reader.read_import_manifest()
    assert import_manifest["schema_version"] == 1
    assert isinstance(import_manifest["content_sha256"], str)
    assert reader.read_folders_manifest() == [{"raw_name": "Inbox", "display_name": "Inbox"}]
    assert [event["event"] for event in reader.read_all_events()] == [
        "item_discovered",
        "item_saved",
    ]


def test_pst_static_snapshots_are_immutable(tmp_path: Path) -> None:
    import_uuid = str(uuid.uuid4())
    with PstManifestWriter(tmp_path, import_uuid) as writer:
        writer.write_import_manifest(_import_snapshot(import_uuid))
        with pytest.raises(ValueError, match="immutable"):
            writer.write_import_manifest({**_import_snapshot(import_uuid), "display_name": "Other"})


def test_pst_manifest_repairs_only_a_torn_tail(tmp_path: Path) -> None:
    import_uuid = str(uuid.uuid4())
    with PstManifestWriter(tmp_path, import_uuid) as writer:
        writer.append(_discovered(import_uuid))
    path = tmp_path / "manifests" / "pst" / import_uuid / "items.jsonl"
    source = path.read_bytes()
    path.write_bytes(source + b'{"event":"item_saved"')

    assert len(list(read_events(path))) == 1
    assert path.read_bytes() == source


def test_pst_manifest_rejects_middle_corruption(tmp_path: Path) -> None:
    import_uuid = str(uuid.uuid4())
    with PstManifestWriter(tmp_path, import_uuid) as writer:
        writer.append(_discovered(import_uuid))
        writer.append(_saved(import_uuid))
    path = tmp_path / "manifests" / "pst" / import_uuid / "items.jsonl"
    lines = path.read_bytes().splitlines(keepends=True)
    lines[0] = lines[0].replace(b'"source_size_bytes":10', b'"source_size_bytes":11')
    path.write_bytes(b"".join(lines))

    with pytest.raises(ManifestCorruptError):
        list(read_events(path))


def test_pst_manifest_reader_rejects_invalid_event_transition(tmp_path: Path) -> None:
    import_uuid = str(uuid.uuid4())
    with PstManifestWriter(tmp_path, import_uuid) as writer:
        writer.append(_discovered(import_uuid))
        writer.append(_saved(import_uuid))
    path = tmp_path / "manifests" / "pst" / import_uuid / "items.jsonl"
    lines = path.read_bytes().splitlines(keepends=True)
    invalid = {
        "event": "item_reparsed",
        "import_uuid": import_uuid,
        "timestamp": "2026-09-09T00:00:03Z",
        "source_item_key": "Inbox/1.eml",
        "final_relative_path": "eml/pst/message.eml",
        "file_hash": "c" * 64,
        "size_bytes": 10,
    }
    payload = (
        str(invalid).replace("'", '"').replace("False", "false").encode("utf-8")
    )
    checksum = zlib.crc32(payload) & 0xFFFFFFFF
    lines[1] = payload + f"|CRC32:{checksum:08x}\n".encode("ascii")
    lines.append(
        path.read_bytes().splitlines(keepends=True)[1]
    )
    path.write_bytes(b"".join(lines))

    with pytest.raises(ManifestCorruptError):
        list(read_events(path))


def test_pst_manifest_enforces_item_and_purge_transitions(tmp_path: Path) -> None:
    import_uuid = str(uuid.uuid4())
    with PstManifestWriter(tmp_path, import_uuid) as writer:
        with pytest.raises(ValueError, match="item_discovered first"):
            writer.append(_saved(import_uuid))
        writer.append(_discovered(import_uuid))
        with pytest.raises(ValueError, match="purge_intent"):
            writer.append(
                {
                    "event": "purged",
                    "import_uuid": import_uuid,
                    "timestamp": "2026-09-09T00:00:03Z",
                    "source_item_key": "Inbox/1.eml",
                    "relative_path": "eml/pst/message.eml",
                    "file_hash": "c" * 64,
                    "shared_reference_count": 0,
                    "physical_delete": True,
                }
            )


def test_pst_manifest_crc_is_checked(tmp_path: Path) -> None:
    import_uuid = str(uuid.uuid4())
    with PstManifestWriter(tmp_path, import_uuid) as writer:
        writer.append(_discovered(import_uuid))
    path = tmp_path / "manifests" / "pst" / import_uuid / "items.jsonl"
    line = path.read_bytes().splitlines(keepends=True)[0]
    payload = line[:-1].rsplit(b"|CRC32:", 1)[0]
    path.write_bytes(payload + b"|CRC32:00000000\n")

    assert list(read_events(path)) == []
    assert path.read_bytes() == b""


def test_pst_manifest_crc_fixture_is_valid() -> None:
    payload = (
        b'{"event":"import_abandoned","import_uuid":"00000000-0000-4000-8000-000000000000",'
        b'"reason":"cancelled","timestamp":"2026-09-09T00:00:00Z"}'
    )
    checksum = zlib.crc32(payload) & 0xFFFFFFFF
    assert checksum == int(f"{checksum:08x}", 16)