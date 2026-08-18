"""Use cases for registering accounts and loading their credentials."""

from __future__ import annotations

from collections.abc import Sequence

from mail_dock.domain.accounts import validate_account_id
from mail_dock.domain.errors import AuthenticationError
from mail_dock.domain.ports import BaseCredentialStore
from mail_dock.domain.repository import BaseMessageRepository, MessageRecord


def register_account(
    repo: BaseMessageRepository,
    credential_store: BaseCredentialStore,
    *,
    account_id: str,
    host: str,
    port: int,
    username: str,
    password: str,
    display_name: str | None,
) -> str:
    """Store credentials outside SQLite and register the connection details."""

    validate_account_id(account_id)
    credential_store.set_password(account_id, password)
    repo.upsert_account(
        {
            "id": account_id,
            "provider_type": "onamae_imap",
            "display_name": display_name,
            "host": host,
            "port": port,
            "username": username,
            "is_enabled": 1,
        }
    )
    return account_id


def update_account(
    repo: BaseMessageRepository,
    credential_store: BaseCredentialStore,
    *,
    account_id: str,
    host: str,
    port: int,
    username: str,
    password: str | None,
    display_name: str | None,
    is_enabled: bool,
) -> str:
    """Update connection details for an existing account without renaming it.

    ``account_id`` is immutable once registered: it is the credential-store
    key and the storage/foreign-key anchor for that account's folders and
    messages, so this function never changes it. ``password`` is left as-is
    in the credential store unless a non-empty replacement is supplied.
    """

    validate_account_id(account_id)
    if password:
        credential_store.set_password(account_id, password)
    repo.upsert_account(
        {
            "id": account_id,
            "provider_type": "onamae_imap",
            "display_name": display_name,
            "host": host,
            "port": port,
            "username": username,
            "is_enabled": int(is_enabled),
        }
    )
    return account_id


def load_credentials(credential_store: BaseCredentialStore, account_id: str) -> str:
    """Load an account password or signal that credentials must be supplied."""

    password = credential_store.get_password(account_id)
    if password is None:
        raise AuthenticationError(f"No credentials are registered for account: {account_id}")
    return password


def list_accounts(repo: BaseMessageRepository) -> Sequence[MessageRecord]:
    """Return registered connection details without reading credential storage."""

    return repo.list_accounts()
