from __future__ import annotations

import pytest

from mail_dock.domain.errors import CredentialStoreError
from mail_dock.infrastructure.security.keyring_store import KeyringCredentialStore


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

    with pytest.raises(CredentialStoreError, match="Could not store"):
        KeyringCredentialStore().set_password("account", "secret")
