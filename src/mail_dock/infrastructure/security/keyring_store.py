"""Keyring-backed credential storage for mail-dock accounts."""

from __future__ import annotations

import keyring

from mail_dock.domain.errors import CredentialStoreError
from mail_dock.domain.ports import BaseCredentialStore

_SERVICE_NAME = "mail-dock"


class KeyringCredentialStore(BaseCredentialStore):
    """Store passwords using the operating system's configured keyring."""

    def set_password(self, account_id: str, password: str) -> None:
        """Store a password without exposing it to the database or logs."""

        try:
            keyring.set_password(_SERVICE_NAME, account_id, password)
        except Exception as error:
            raise CredentialStoreError("Could not store account credentials") from error

    def get_password(self, account_id: str) -> str | None:
        """Return a stored password, translating backend failures."""

        try:
            return keyring.get_password(_SERVICE_NAME, account_id)
        except Exception as error:
            raise CredentialStoreError("Could not read account credentials") from error

    def delete_password(self, account_id: str) -> None:
        """Delete a stored password, translating backend failures."""

        try:
            keyring.delete_password(_SERVICE_NAME, account_id)
        except Exception as error:
            raise CredentialStoreError("Could not delete account credentials") from error