from __future__ import annotations

from pathlib import Path

import pytest

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


def test_new_session_store_requires_credentials_again() -> None:
    first_store = SessionCredentialStore()
    first_store.set_password("account", "secret")

    restarted_store = SessionCredentialStore()

    assert restarted_store.get_password("account") is None
    restarted_store.set_password("account", "reentered-secret")
    assert restarted_store.get_password("account") == "reentered-secret"
    assert first_store.get_password("account") == "secret"


def test_session_store_repr_does_not_expose_account_or_password() -> None:
    store = SessionCredentialStore()
    store.set_password("private-account", "private-secret")

    representation = repr(store)

    assert "private-account" not in representation
    assert "private-secret" not in representation


def test_session_store_does_not_create_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tmp_path = tmp_path / "working-directory"
    tmp_path.mkdir()
    monkeypatch.chdir(tmp_path)

    before = set(tmp_path.iterdir())

    store = SessionCredentialStore()
    store.set_password("account", "secret")
    store.get_password("account")
    store.delete_password("account")

    assert set(tmp_path.iterdir()) == before
