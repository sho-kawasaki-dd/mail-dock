"""Read and write mail-dock configuration.

This module owns configuration persistence only. It does not construct
application objects or perform dependency injection.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from platformdirs import user_config_dir

from mail_dock.domain.errors import ConfigError, ConfigVersionTooNewError

CURRENT_SCHEMA_VERSION = 1
CONFIG_FILENAME = "config.json"

REMOTE_DELETE_MODES = frozenset({"trash", "permanent"})
PURGE_MODES = frozenset({"manual", "grace", "immediate"})
STARTUP_VERIFICATION_MODES = frozenset({"quick", "full"})

type JSONValue = str | int | float | bool | list[JSONValue] | dict[str, JSONValue] | None


def config_dir() -> Path:
    """Return the platform-specific directory containing configuration."""

    return Path(user_config_dir("mail-dock", appauthor=False))


def config_path() -> Path:
    """Return the path of the persistent JSON configuration file."""

    return config_dir() / CONFIG_FILENAME


@dataclass(frozen=True)
class AppConfig:
    """Validated application settings persisted in ``config.json``."""

    schema_version: int = CURRENT_SCHEMA_VERSION
    storage_root_candidates: tuple[str, ...] = ()
    storage_root_uuid: str | None = None
    sync_on_startup: bool = True
    sync_interval_minutes: int = 60
    max_message_bytes: int = 50 * 1024 * 1024
    remote_delete_mode: str = "trash"
    remote_trash_folder: str | None = None
    delete_batch_limit: int = 1000
    trash_grace_days: int = 30
    purge_mode: str = "manual"
    block_remote_images: bool = True
    startup_verification: str = "quick"
    heartbeat_interval_sec: int = 5
    reprobe_attempts: int = 3
    sync_log_retention_days: int = 90
    db_backup_to_local_disk: bool = False
    extra: Mapping[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Reject invalid values even when a config is created in memory."""

        _validate_config(self)


type _ConfigUpgrade = Callable[[dict[str, JSONValue]], dict[str, JSONValue]]


def _upgrade_v0_to_v1(data: dict[str, JSONValue]) -> dict[str, JSONValue]:
    upgraded = dict(data)
    upgraded["schema_version"] = CURRENT_SCHEMA_VERSION
    return upgraded


_CONFIG_UPGRADERS: dict[int, _ConfigUpgrade] = {0: _upgrade_v0_to_v1}


_KNOWN_FIELDS = frozenset(
    {
        "schema_version",
        "storage_root_candidates",
        "storage_root_uuid",
        "sync_on_startup",
        "sync_interval_minutes",
        "max_message_bytes",
        "remote_delete_mode",
        "remote_trash_folder",
        "delete_batch_limit",
        "trash_grace_days",
        "purge_mode",
        "block_remote_images",
        "startup_verification",
        "heartbeat_interval_sec",
        "reprobe_attempts",
        "sync_log_retention_days",
        "db_backup_to_local_disk",
    }
)


def _config_error(message: str) -> ConfigError:
    return ConfigError(f"Invalid configuration: {message}")


def _require_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _config_error(f"{field_name} must be an integer")
    return value


def _require_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise _config_error(f"{field_name} must be a boolean")
    return value


def _require_string(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise _config_error(f"{field_name} must be a string")
    return value


def _require_optional_string(value: object, field_name: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise _config_error(f"{field_name} must be a string or null")
    return value


def _require_non_negative(value: object, field_name: str) -> int:
    number = _require_int(value, field_name)
    if number < 0:
        raise _config_error(f"{field_name} must not be negative")
    return number


def _require_positive(value: object, field_name: str) -> int:
    number = _require_int(value, field_name)
    if number <= 0:
        raise _config_error(f"{field_name} must be greater than zero")
    return number


def _require_mode(value: object, field_name: str, allowed: frozenset[str]) -> str:
    mode = _require_string(value, field_name)
    if mode not in allowed:
        choices = ", ".join(sorted(allowed))
        raise _config_error(f"{field_name} must be one of: {choices}")
    return mode


def _validate_config(config: AppConfig) -> None:
    schema_version = _require_int(config.schema_version, "schema_version")
    if schema_version < 1:
        raise _config_error("schema_version must be at least 1")

    if not isinstance(config.storage_root_candidates, tuple):
        raise _config_error("storage_root_candidates must be a tuple of strings")
    for candidate in config.storage_root_candidates:
        if not isinstance(candidate, str) or not candidate:
            raise _config_error("storage_root_candidates must contain non-empty strings")

    _require_optional_string(config.storage_root_uuid, "storage_root_uuid")
    _require_bool(config.sync_on_startup, "sync_on_startup")
    _require_non_negative(config.sync_interval_minutes, "sync_interval_minutes")
    _require_positive(config.max_message_bytes, "max_message_bytes")
    _require_mode(config.remote_delete_mode, "remote_delete_mode", REMOTE_DELETE_MODES)
    _require_optional_string(config.remote_trash_folder, "remote_trash_folder")
    _require_positive(config.delete_batch_limit, "delete_batch_limit")
    _require_non_negative(config.trash_grace_days, "trash_grace_days")
    _require_mode(config.purge_mode, "purge_mode", PURGE_MODES)
    _require_bool(config.block_remote_images, "block_remote_images")
    _require_mode(config.startup_verification, "startup_verification", STARTUP_VERIFICATION_MODES)
    _require_positive(config.heartbeat_interval_sec, "heartbeat_interval_sec")
    _require_non_negative(config.reprobe_attempts, "reprobe_attempts")
    _require_non_negative(config.sync_log_retention_days, "sync_log_retention_days")
    _require_bool(config.db_backup_to_local_disk, "db_backup_to_local_disk")


def _as_object(value: object) -> dict[str, JSONValue]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise _config_error("the JSON root must be an object")
    return cast(dict[str, JSONValue], value)


def _as_string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _config_error(f"{field_name} must be an array of strings")
    return tuple(value)


def _upgrade(data: dict[str, JSONValue]) -> dict[str, JSONValue]:
    raw_version = data.get("schema_version", CURRENT_SCHEMA_VERSION)
    version = _require_int(raw_version, "schema_version")
    if version > CURRENT_SCHEMA_VERSION:
        raise ConfigVersionTooNewError(
            f"Configuration schema {version} is newer than supported schema "
            f"{CURRENT_SCHEMA_VERSION}"
        )
    upgraded = dict(data)
    while version < CURRENT_SCHEMA_VERSION:
        upgrader = _CONFIG_UPGRADERS.get(version)
        if upgrader is None:
            raise _config_error(f"no upgrade path exists from schema {version}")
        upgraded = upgrader(upgraded)
        version = _require_int(upgraded.get("schema_version"), "schema_version")
    return upgraded


def _decode(data: dict[str, JSONValue]) -> AppConfig:
    upgraded = _upgrade(data)
    defaults = AppConfig()
    known = {key: value for key, value in upgraded.items() if key in _KNOWN_FIELDS}
    extra = {key: value for key, value in upgraded.items() if key not in _KNOWN_FIELDS}
    return AppConfig(
        schema_version=_require_int(
            known.get("schema_version", defaults.schema_version), "schema_version"
        ),
        storage_root_candidates=_as_string_tuple(
            known.get("storage_root_candidates", list(defaults.storage_root_candidates)),
            "storage_root_candidates",
        ),
        storage_root_uuid=_require_optional_string(
            known.get("storage_root_uuid", defaults.storage_root_uuid), "storage_root_uuid"
        ),
        sync_on_startup=_require_bool(
            known.get("sync_on_startup", defaults.sync_on_startup), "sync_on_startup"
        ),
        sync_interval_minutes=_require_int(
            known.get("sync_interval_minutes", defaults.sync_interval_minutes),
            "sync_interval_minutes",
        ),
        max_message_bytes=_require_int(
            known.get("max_message_bytes", defaults.max_message_bytes), "max_message_bytes"
        ),
        remote_delete_mode=_require_string(
            known.get("remote_delete_mode", defaults.remote_delete_mode), "remote_delete_mode"
        ),
        remote_trash_folder=_require_optional_string(
            known.get("remote_trash_folder", defaults.remote_trash_folder), "remote_trash_folder"
        ),
        delete_batch_limit=_require_int(
            known.get("delete_batch_limit", defaults.delete_batch_limit), "delete_batch_limit"
        ),
        trash_grace_days=_require_int(
            known.get("trash_grace_days", defaults.trash_grace_days), "trash_grace_days"
        ),
        purge_mode=_require_string(known.get("purge_mode", defaults.purge_mode), "purge_mode"),
        block_remote_images=_require_bool(
            known.get("block_remote_images", defaults.block_remote_images), "block_remote_images"
        ),
        startup_verification=_require_string(
            known.get("startup_verification", defaults.startup_verification),
            "startup_verification",
        ),
        heartbeat_interval_sec=_require_int(
            known.get("heartbeat_interval_sec", defaults.heartbeat_interval_sec),
            "heartbeat_interval_sec",
        ),
        reprobe_attempts=_require_int(
            known.get("reprobe_attempts", defaults.reprobe_attempts), "reprobe_attempts"
        ),
        sync_log_retention_days=_require_int(
            known.get("sync_log_retention_days", defaults.sync_log_retention_days),
            "sync_log_retention_days",
        ),
        db_backup_to_local_disk=_require_bool(
            known.get("db_backup_to_local_disk", defaults.db_backup_to_local_disk),
            "db_backup_to_local_disk",
        ),
        extra=extra,
    )


def load() -> AppConfig:
    """Load and validate the persistent configuration, or return defaults."""

    path = config_path()
    try:
        with path.open(encoding="utf-8") as config_file:
            raw = json.load(config_file)
    except FileNotFoundError:
        return AppConfig()
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(f"Could not read configuration: {error}") from error
    return _decode(_as_object(raw))


def _encode(config: AppConfig) -> dict[str, JSONValue]:
    _validate_config(config)
    values: dict[str, JSONValue] = dict(config.extra)
    values.update(
        {
            "schema_version": config.schema_version,
            "storage_root_candidates": list(config.storage_root_candidates),
            "storage_root_uuid": config.storage_root_uuid,
            "sync_on_startup": config.sync_on_startup,
            "sync_interval_minutes": config.sync_interval_minutes,
            "max_message_bytes": config.max_message_bytes,
            "remote_delete_mode": config.remote_delete_mode,
            "remote_trash_folder": config.remote_trash_folder,
            "delete_batch_limit": config.delete_batch_limit,
            "trash_grace_days": config.trash_grace_days,
            "purge_mode": config.purge_mode,
            "block_remote_images": config.block_remote_images,
            "startup_verification": config.startup_verification,
            "heartbeat_interval_sec": config.heartbeat_interval_sec,
            "reprobe_attempts": config.reprobe_attempts,
            "sync_log_retention_days": config.sync_log_retention_days,
            "db_backup_to_local_disk": config.db_backup_to_local_disk,
        }
    )
    return values


def save(config: AppConfig) -> None:
    """Atomically write a validated configuration to its JSON file."""

    path = config_path()
    parent = path.parent
    payload = _encode(config)
    temporary_path: Path | None = None
    try:
        parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            text=True,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
            json.dump(payload, temporary_file, ensure_ascii=False, indent=2, sort_keys=True)
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)  # noqa: PTH105
        temporary_path = None
    except (OSError, TypeError, ValueError) as error:
        raise ConfigError(f"Could not write configuration: {error}") from error
    finally:
        if temporary_path is not None:
            with suppress(FileNotFoundError):
                temporary_path.unlink()
