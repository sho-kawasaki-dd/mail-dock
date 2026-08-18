from __future__ import annotations

import pytest

from mail_dock.domain.errors import AuthenticationError
from mail_dock.domain.ports import BaseCredentialStore
from mail_dock.usecases.register_account import (
    list_accounts,
    load_credentials,
    register_account,
    update_account,
)
from tests.support.in_memory_repository import InMemoryMessageRepository


class FakeCredentialStore(BaseCredentialStore):
    def __init__(self) -> None:
        self.passwords: dict[str, str] = {}

    def set_password(self, account_id: str, password: str) -> None:
        self.passwords[account_id] = password

    def get_password(self, account_id: str) -> str | None:
        return self.passwords.get(account_id)

    def delete_password(self, account_id: str) -> None:
        self.passwords.pop(account_id, None)


def test_register_account_keeps_password_out_of_repository() -> None:
    repository = InMemoryMessageRepository()
    credentials = FakeCredentialStore()

    account_id = register_account(
        repository,
        credentials,
        account_id="user@example.com",
        host="imap.example.com",
        port=993,
        username="user@example.com",
        password="secret",
        display_name="Example",
    )

    assert account_id == "user@example.com"
    assert credentials.passwords == {account_id: "secret"}
    assert repository.list_accounts() == [
        {
            "id": account_id,
            "provider_type": "onamae_imap",
            "display_name": "Example",
            "host": "imap.example.com",
            "port": 993,
            "username": "user@example.com",
            "is_enabled": 1,
        }
    ]
    assert "password" not in repository.list_accounts()[0]


@pytest.mark.parametrize("account_id", ["", ".", "../account", "CON", "user:"])
def test_register_account_rejects_unsafe_account_id(account_id: str) -> None:
    repository = InMemoryMessageRepository()
    credentials = FakeCredentialStore()

    with pytest.raises(ValueError):
        register_account(
            repository,
            credentials,
            account_id=account_id,
            host="imap.example.com",
            port=993,
            username="user",
            password="secret",
            display_name=None,
        )

    assert repository.list_accounts() == []
    assert credentials.passwords == {}


def test_load_credentials_requires_registered_password() -> None:
    with pytest.raises(AuthenticationError):
        load_credentials(FakeCredentialStore(), "missing")


def test_list_accounts_delegates_without_credentials() -> None:
    repository = InMemoryMessageRepository()
    repository.upsert_account({"id": "account", "username": "user"})

    assert list_accounts(repository) == repository.list_accounts()


def test_update_account_keeps_existing_password_when_blank() -> None:
    repository = InMemoryMessageRepository()
    credentials = FakeCredentialStore()
    register_account(
        repository,
        credentials,
        account_id="user@example.com",
        host="imap.example.com",
        port=993,
        username="user@example.com",
        password="secret",
        display_name=None,
    )

    update_account(
        repository,
        credentials,
        account_id="user@example.com",
        host="imap.example.com",
        port=993,
        username="user@example.com",
        password=None,
        display_name="\u4ed5\u4e8b",
        is_enabled=True,
    )

    assert credentials.passwords == {"user@example.com": "secret"}
    account = repository.list_accounts()[0]
    assert account["display_name"] == "\u4ed5\u4e8b"
    assert account["id"] == "user@example.com"


def test_update_account_replaces_password_when_supplied() -> None:
    repository = InMemoryMessageRepository()
    credentials = FakeCredentialStore()
    register_account(
        repository,
        credentials,
        account_id="account",
        host="imap.example.com",
        port=993,
        username="user",
        password="old-secret",
        display_name=None,
    )

    update_account(
        repository,
        credentials,
        account_id="account",
        host="imap.example.com",
        port=993,
        username="user",
        password="new-secret",
        display_name=None,
        is_enabled=True,
    )

    assert credentials.passwords == {"account": "new-secret"}


def test_update_account_preserves_is_enabled_flag() -> None:
    repository = InMemoryMessageRepository()
    credentials = FakeCredentialStore()
    repository.upsert_account(
        {
            "id": "account",
            "provider_type": "onamae_imap",
            "display_name": None,
            "host": "imap.example.com",
            "port": 993,
            "username": "user",
            "is_enabled": 0,
        }
    )

    update_account(
        repository,
        credentials,
        account_id="account",
        host="imap.example.com",
        port=993,
        username="user",
        password=None,
        display_name="renamed",
        is_enabled=False,
    )

    assert repository.list_accounts()[0]["is_enabled"] == 0
