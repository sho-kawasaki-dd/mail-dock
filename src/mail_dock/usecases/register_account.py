"""Use cases for registering accounts and loading their credentials."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from mail_dock.domain.accounts import validate_account_id
from mail_dock.domain.errors import AuthenticationError
from mail_dock.domain.ports import BaseCredentialStore, BaseManifestReader, BaseManifestWriter
from mail_dock.domain.repository import BaseMessageRepository, MessageRecord
from mail_dock.usecases.snapshots import record_account_snapshot


def _validate_auth_configuration(
    auth_type: Literal["password", "xoauth2"],
    oauth_provider: str | None,
    oauth_client_id: str | None,
    oauth_tenant: str | None,
) -> None:
    if auth_type not in {"password", "xoauth2"}:
        raise ValueError("auth_type must be 'password' or 'xoauth2'")
    if auth_type == "xoauth2":
        if oauth_provider not in {"google", "microsoft"} or not oauth_client_id:
            raise ValueError("OAuth accounts require a supported provider and client ID")
        if oauth_provider == "google" and oauth_tenant is not None:
            raise ValueError("Google OAuth does not accept a tenant")
    elif any(value is not None for value in (oauth_provider, oauth_client_id, oauth_tenant)):
        raise ValueError("OAuth settings require auth_type='xoauth2'")


def register_account(
    repo: BaseMessageRepository,
    credential_store: BaseCredentialStore,
    *,
    account_id: str,
    host: str,
    port: int,
    username: str,
    password: str | None = None,
    display_name: str | None,
    tls_mode: Literal["implicit", "starttls"] = "implicit",
    ca_cert_path: str | None = None,
    auth_type: Literal["password", "xoauth2"] = "password",
    oauth_provider: str | None = None,
    oauth_client_id: str | None = None,
    oauth_tenant: str | None = None,
    manifest: BaseManifestWriter | None = None,
    manifest_reader: BaseManifestReader | None = None,
) -> str:
    """Store credentials outside SQLite and register the connection details."""

    validate_account_id(account_id)
    _validate_auth_configuration(auth_type, oauth_provider, oauth_client_id, oauth_tenant)
    if auth_type == "password":
        if not password:
            raise ValueError("password is required for password authentication")
        credential_store.set_password(account_id, password)
    elif password:
        raise ValueError("password must not be supplied for OAuth authentication")
    account = {
        "id": account_id,
        "provider_type": "imap",
        "display_name": display_name,
        "host": host,
        "port": port,
        "username": username,
        "is_enabled": 1,
        "tls_mode": tls_mode,
        "ca_cert_path": ca_cert_path,
        "auth_type": auth_type,
        "oauth_provider": oauth_provider,
        "oauth_client_id": oauth_client_id,
        "oauth_tenant": oauth_tenant,
    }
    if manifest is not None and manifest_reader is not None:
        record_account_snapshot(manifest, manifest_reader, account)
    repo.upsert_account(account)
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
    tls_mode: Literal["implicit", "starttls"] = "implicit",
    ca_cert_path: str | None = None,
    auth_type: Literal["password", "xoauth2"] = "password",
    oauth_provider: str | None = None,
    oauth_client_id: str | None = None,
    oauth_tenant: str | None = None,
    manifest: BaseManifestWriter | None = None,
    manifest_reader: BaseManifestReader | None = None,
) -> str:
    """Update connection details for an existing account without renaming it.

    ``account_id`` is immutable once registered: it is the credential-store
    key and the storage/foreign-key anchor for that account's folders and
    messages, so this function never changes it. ``password`` is left as-is
    in the credential store unless a non-empty replacement is supplied.
    """

    validate_account_id(account_id)
    _validate_auth_configuration(auth_type, oauth_provider, oauth_client_id, oauth_tenant)
    if auth_type == "xoauth2" and password:
        raise ValueError("password must not be supplied for OAuth authentication")
    if auth_type == "password" and password:
        credential_store.set_password(account_id, password)
    account = {
        "id": account_id,
        "provider_type": "imap",
        "display_name": display_name,
        "host": host,
        "port": port,
        "username": username,
        "is_enabled": int(is_enabled),
        "tls_mode": tls_mode,
        "ca_cert_path": ca_cert_path,
        "auth_type": auth_type,
        "oauth_provider": oauth_provider,
        "oauth_client_id": oauth_client_id,
        "oauth_tenant": oauth_tenant,
    }
    if manifest is not None and manifest_reader is not None:
        record_account_snapshot(manifest, manifest_reader, account)
    repo.upsert_account(account)
    return account_id


def load_credentials(credential_store: BaseCredentialStore, account_id: str) -> str:
    """Load an account password or signal that credentials must be supplied."""

    password = credential_store.get_password(account_id)
    if password is None:
        raise AuthenticationError(f"No credentials are registered for account: {account_id}")
    return password


def list_accounts(repo: BaseMessageRepository) -> Sequence[MessageRecord]:
    """Return registered connection details without reading credential storage."""

    return [
        {key: value for key, value in account.items() if key != "password"}
        for account in repo.list_accounts()
    ]
