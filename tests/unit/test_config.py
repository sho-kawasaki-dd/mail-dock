import json
import os
from pathlib import Path

import pytest

from mail_dock import config as config_module
from mail_dock.domain.errors import ConfigError, ConfigVersionTooNewError


@pytest.fixture
def config_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "config_path", lambda: path)
    return path


def test_load_returns_defaults_when_file_is_missing(config_path: Path) -> None:
    config = config_module.load()

    assert config.schema_version == 2
    assert config.storage_root_candidates == ()
    assert config.heartbeat_interval_sec == 5
    assert config.db_backup_to_local_disk is False
    assert config.storage_profiles == {}
    assert config.credential_storage == "keyring"


def test_save_and_load_preserve_unknown_keys(config_path: Path) -> None:
    config = config_module.AppConfig(
        storage_root_candidates=("/mnt/mail",),
        storage_root_uuid="root-uuid",
        extra={"future_setting": {"enabled": True}},
    )

    config_module.save(config)
    loaded = config_module.load()

    assert loaded == config
    assert json.loads(config_path.read_text(encoding="utf-8"))["future_setting"] == {
        "enabled": True
    }


def test_load_rejects_future_schema(config_path: Path) -> None:
    config_path.write_text(json.dumps({"schema_version": 3}), encoding="utf-8")

    with pytest.raises(ConfigVersionTooNewError):
        config_module.load()


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("sync_interval_minutes", -1),
        ("max_message_bytes", 0),
        ("remote_delete_mode", "invalid"),
        ("heartbeat_interval_sec", 0),
        ("sync_on_startup", "yes"),
    ],
)
def test_load_rejects_invalid_values(config_path: Path, field_name: str, value: object) -> None:
    config_path.write_text(json.dumps({field_name: value}), encoding="utf-8")

    with pytest.raises(ConfigError):
        config_module.load()


def test_load_rejects_invalid_json(config_path: Path) -> None:
    config_path.write_text("not-json", encoding="utf-8")

    with pytest.raises(ConfigError):
        config_module.load()


def test_v1_config_upgrades_without_overwriting_existing_values(config_path: Path) -> None:
    config_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "storage_profiles": {"root-uuid": {"encryption": "encrypted"}},
                "credential_storage": "session_only",
                "future_setting": {"enabled": True},
            }
        ),
        encoding="utf-8",
    )

    loaded = config_module.load()

    assert loaded.schema_version == 2
    assert loaded.storage_profiles == {"root-uuid": {"encryption": "encrypted"}}
    assert loaded.credential_storage == "session_only"
    assert loaded.extra == {"future_setting": {"enabled": True}}


def test_v0_config_upgrades_through_v1_to_v2(config_path: Path) -> None:
    config_path.write_text(json.dumps({"schema_version": 0}), encoding="utf-8")

    loaded = config_module.load()

    assert loaded.schema_version == 2
    assert loaded.storage_profiles == {}
    assert loaded.credential_storage == "keyring"


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("storage_profiles", []),
        ("storage_profiles", {"root-uuid": "not-an-object"}),
        ("storage_profiles", {"root-uuid": {"capability_level": "invalid"}}),
        ("storage_profiles", {"root-uuid": {"encryption": "invalid"}}),
        ("credential_storage", "invalid"),
    ],
)
def test_load_rejects_invalid_phase_3_5_config_values(
    config_path: Path, field_name: str, value: object
) -> None:
    config_path.write_text(json.dumps({field_name: value}), encoding="utf-8")

    with pytest.raises(ConfigError):
        config_module.load()


def test_save_and_load_round_trip_phase_3_5_fields(config_path: Path) -> None:
    config = config_module.AppConfig(
        storage_profiles={
            "root-uuid": {
                "capabilities": {"wal_supported": True},
                "capability_level": "ok",
                "checked_path": "/mnt/mail",
                "storage_fingerprint": "posix:1:/mnt/mail",
                "capability_ack_at": None,
                "encryption": "unknown",
                "encryption_declared_at": "2026-08-05T00:00:00+00:00",
                "first_sync_confirmed_at": None,
            }
        },
        credential_storage="session_only",
    )

    config_module.save(config)

    assert config_module.load() == config


def test_failed_replace_keeps_previous_file(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_module.save(config_module.AppConfig())
    original = config_path.read_text(encoding="utf-8")

    def fail_replace(source: Path, target: Path) -> None:
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(ConfigError):
        config_module.save(config_module.AppConfig(sync_interval_minutes=30))

    assert config_path.read_text(encoding="utf-8") == original
