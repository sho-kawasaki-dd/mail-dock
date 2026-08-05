from __future__ import annotations

import keyring
import pytest

from mail_dock.domain.errors import CredentialStoreError
from mail_dock.infrastructure.security import keyring_store
from mail_dock.infrastructure.security.keyring_store import (
    KeyringBackendStatus,
    KeyringCredentialStore,
)


class SupportedBackend:
    __module__ = "keyring.backends.SecretService"
    __qualname__ = "Keyring"


def test_detect_backend_uses_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keyring, "get_keyring", SupportedBackend)

    assert keyring_store.detect_backend() is KeyringBackendStatus.SUPPORTED
    assert keyring_store.backend_name() == "keyring.backends.SecretService.Keyring"


def test_detect_backend_rejects_unknown_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    class UnknownBackend:
        pass

    monkeypatch.setattr(keyring, "get_keyring", UnknownBackend)

    assert keyring_store.detect_backend() is KeyringBackendStatus.UNSUPPORTED


def test_detect_backend_reports_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail() -> object:
        raise RuntimeError("backend unavailable")

    monkeypatch.setattr(keyring, "get_keyring", fail)

    assert keyring_store.detect_backend() is KeyringBackendStatus.UNAVAILABLE
    assert keyring_store.backend_name() == "unavailable"


def test_keyring_store_saves_loads_and_deletes(monkeypatch: pytest.MonkeyPatch) -> None:
    passwords: dict[tuple[str, str], str] = {}

    def set_password(service: str, account_id: str, password: str) -> None:
        passwords[(service, account_id)] = password

    def get_password(service: str, account_id: str) -> str | None:
        return passwords.get((service, account_id))

    def delete_password(service: str, account_id: str) -> None:
        passwords.pop((service, account_id), None)

    monkeypatch.setattr(
        "mail_dock.infrastructure.security.keyring_store.keyring.set_password", set_password
    )
    monkeypatch.setattr(
        "mail_dock.infrastructure.security.keyring_store.keyring.get_password", get_password
    )
    monkeypatch.setattr(
        "mail_dock.infrastructure.security.keyring_store.keyring.delete_password",
        delete_password,
    )
    monkeypatch.setattr(
        "mail_dock.infrastructure.security.keyring_store.keyring.get_keyring",
        SupportedBackend,
    )

    store = KeyringCredentialStore()
    store.set_password("account", "secret")
    assert store.get_password("account") == "secret"

    store.delete_password("account")
    assert store.get_password("account") is None


def test_keyring_store_wraps_backend_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("backend unavailable")

    monkeypatch.setattr(
        "mail_dock.infrastructure.security.keyring_store.keyring.set_password", fail
    )
    monkeypatch.setattr(
        "mail_dock.infrastructure.security.keyring_store.keyring.get_keyring",
        SupportedBackend,
    )

    with pytest.raises(CredentialStoreError, match="Could not store"):
        KeyringCredentialStore().set_password("account", "secret")


def test_keyring_store_does_not_write_to_unsupported_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str]] = []

    class UnknownBackend:
        pass

    def set_password(service: str, account_id: str, password: str) -> None:
        calls.append((service, account_id, password))

    monkeypatch.setattr(
        "mail_dock.infrastructure.security.keyring_store.keyring.get_keyring",
        UnknownBackend,
    )
    monkeypatch.setattr(
        "mail_dock.infrastructure.security.keyring_store.keyring.set_password",
        set_password,
    )

    with pytest.raises(CredentialStoreError, match="will not be stored"):
        KeyringCredentialStore().set_password("account", "secret")

    assert calls == []
