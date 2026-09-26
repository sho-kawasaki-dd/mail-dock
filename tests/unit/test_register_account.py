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
        self.secrets: dict[tuple[str, str], str] = {}

    def set_password(self, account_id: str, password: str) -> None:
        self.set_secret(account_id, "password", password)

    def get_password(self, account_id: str) -> str | None:
        return self.get_secret(account_id, "password")

    def delete_password(self, account_id: str) -> None:
        self.delete_secret(account_id, "password")

    def set_secret(self, account_id: str, name: str, value: str) -> None:
        self.secrets[(account_id, name)] = value

    def get_secret(self, account_id: str, name: str) -> str | None:
        return self.secrets.get((account_id, name))

    def delete_secret(self, account_id: str, name: str) -> None:
        self.secrets.pop((account_id, name), None)

    def delete_all_secrets(self, account_id: str) -> None:
        for key in tuple(self.secrets):
            if key[0] == account_id:
                del self.secrets[key]


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
    assert credentials.secrets == {(account_id, "password"): "secret"}
    assert repository.list_accounts() == [
        {
            "id": account_id,
            "provider_type": "imap",
            "display_name": "Example",
            "host": "imap.example.com",
            "port": 993,
            "username": "user@example.com",
            "is_enabled": 1,
            "tls_mode": "implicit",
            "ca_cert_path": None,
            "auth_type": "password",
            "oauth_provider": None,
            "oauth_client_id": None,
            "oauth_tenant": None,
        }
    ]
    assert "password" not in repository.list_accounts()[0]


def test_register_oauth_account_does_not_require_or_store_password() -> None:
    repository = InMemoryMessageRepository()
    credentials = FakeCredentialStore()

    register_account(
        repository,
        credentials,
        account_id="user@gmail.com",
        host="imap.gmail.com",
        port=993,
        username="user@gmail.com",
        display_name="Gmail",
        auth_type="xoauth2",
        oauth_provider="google",
        oauth_client_id="desktop-client-id",
    )

    account = repository.list_accounts()[0]
    assert account["auth_type"] == "xoauth2"
    assert account["oauth_provider"] == "google"
    assert account["oauth_client_id"] == "desktop-client-id"
    assert credentials.secrets == {}


def test_register_oauth_account_requires_supported_provider_and_client_id() -> None:
    with pytest.raises(ValueError, match="supported provider and client ID"):
        register_account(
            InMemoryMessageRepository(),
            FakeCredentialStore(),
            account_id="user@gmail.com",
            host="imap.gmail.com",
            port=993,
            username="user@gmail.com",
            display_name=None,
            auth_type="xoauth2",
            oauth_provider="attacker",
            oauth_client_id="client-id",
        )


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
    assert credentials.secrets == {}


def test_load_credentials_requires_registered_password() -> None:
    with pytest.raises(AuthenticationError):
        load_credentials(FakeCredentialStore(), "missing")


def test_list_accounts_includes_connection_settings_without_credentials() -> None:
    repository = InMemoryMessageRepository()
    repository.upsert_account(
        {
            "id": "account",
            "provider_type": "imap",
            "host": "imap.example.com",
            "port": 143,
            "username": "user",
            "tls_mode": "starttls",
            "ca_cert_path": "/etc/ssl/mail-ca.pem",
            "password": "must-not-be-returned",
        }
    )

    accounts = list_accounts(repository)

    assert accounts == [
        {
            "id": "account",
            "provider_type": "imap",
            "host": "imap.example.com",
            "port": 143,
            "username": "user",
            "tls_mode": "starttls",
            "ca_cert_path": "/etc/ssl/mail-ca.pem",
            "auth_type": "password",
            "oauth_provider": None,
            "oauth_client_id": None,
            "oauth_tenant": None,
        }
    ]
    assert "password" not in accounts[0]


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

    assert credentials.secrets == {("user@example.com", "password"): "secret"}
    account = repository.list_accounts()[0]
    assert account["display_name"] == "\u4ed5\u4e8b"
    assert account["id"] == "user@example.com"


def test_update_account_supports_oauth_without_password() -> None:
    repository = InMemoryMessageRepository()
    credentials = FakeCredentialStore()
    register_account(
        repository,
        credentials,
        account_id="account",
        host="imap.example.com",
        port=993,
        username="user@example.com",
        password="secret",
        display_name=None,
    )

    update_account(
        repository,
        credentials,
        account_id="account",
        host="imap.gmail.com",
        port=993,
        username="user@gmail.com",
        password=None,
        display_name="Gmail",
        is_enabled=True,
        auth_type="xoauth2",
        oauth_provider="google",
        oauth_client_id="desktop-client-id",
    )

    account = repository.list_accounts()[0]
    assert account["auth_type"] == "xoauth2"
    assert account["oauth_provider"] == "google"
    assert account["oauth_client_id"] == "desktop-client-id"


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

    assert credentials.secrets == {("account", "password"): "new-secret"}


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
