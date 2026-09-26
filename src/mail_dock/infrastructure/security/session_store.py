"""Process-local credential storage.

Credentials stored here are lost when the process exits. Non-persistence is
intentional: this store never writes credentials to files, databases, or logs.
"""

from __future__ import annotations

from mail_dock.domain.ports import MANAGED_SECRET_NAMES, BaseCredentialStore


class SessionCredentialStore(BaseCredentialStore):
    """Keep credentials only in memory for the lifetime of this instance."""

    def __init__(self) -> None:
        self._secrets: dict[tuple[str, str], str] = {}

    def set_password(self, account_id: str, password: str) -> None:
        """Store a password in this process-local session."""

        self.set_secret(account_id, "password", password)

    def get_password(self, account_id: str) -> str | None:
        """Return a session password, or ``None`` when it is not stored."""

        return self.get_secret(account_id, "password")

    def delete_password(self, account_id: str) -> None:
        """Remove a password from this process-local session."""

        self.delete_secret(account_id, "password")

    def set_secret(self, account_id: str, name: str, value: str) -> None:
        """Store one named secret in this process-local session."""

        self._secrets[(account_id, name)] = value

    def get_secret(self, account_id: str, name: str) -> str | None:
        """Return one session secret, or ``None`` when it is not stored."""

        return self._secrets.get((account_id, name))

    def delete_secret(self, account_id: str, name: str) -> None:
        """Remove one named session secret; absent keys are ignored."""

        self._secrets.pop((account_id, name), None)

    def delete_all_secrets(self, account_id: str) -> None:
        """Remove all currently managed secret names for an account."""

        for name in MANAGED_SECRET_NAMES:
            self.delete_secret(account_id, name)

    def __repr__(self) -> str:
        """Avoid exposing stored credentials or account identifiers."""

        return "SessionCredentialStore()"
