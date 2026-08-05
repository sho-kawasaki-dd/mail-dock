from __future__ import annotations

from pathlib import Path

from mail_dock.infrastructure.security.session_store import SessionCredentialStore


def test_session_store_saves_loads_and_deletes() -> None:
    store = SessionCredentialStore()

    assert store.get_password("account") is None
    store.set_password("account", "secret")
    assert store.get_password("account") == "secret"

    store.delete_password("account")
    assert store.get_password("account") is None


def test_session_store_isolated_between_instances() -> None:
    first_store = SessionCredentialStore()
    second_store = SessionCredentialStore()

    first_store.set_password("account", "secret")

    assert second_store.get_password("account") is None


def test_session_store_repr_does_not_expose_account_or_password() -> None:
    store = SessionCredentialStore()
    store.set_password("private-account", "private-secret")

    representation = repr(store)

    assert "private-account" not in representation
    assert "private-secret" not in representation


def test_session_store_does_not_create_files(tmp_path: Path) -> None:
    before = set(tmp_path.iterdir())

    store = SessionCredentialStore()
    store.set_password("account", "secret")
    store.get_password("account")
    store.delete_password("account")

    assert set(tmp_path.iterdir()) == before
