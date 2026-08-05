"""Process-local credential storage.

Credentials stored here are lost when the process exits. Non-persistence is
intentional: this store never writes credentials to files, databases, or logs.
"""

from __future__ import annotations

from mail_dock.domain.ports import BaseCredentialStore


class SessionCredentialStore(BaseCredentialStore):
    """Keep credentials only in memory for the lifetime of this instance."""

    def __init__(self) -> None:
        self._passwords: dict[str, str] = {}

    def set_password(self, account_id: str, password: str) -> None:
        """Store a password in this process-local session."""

        self._passwords[account_id] = password

    def get_password(self, account_id: str) -> str | None:
        """Return a session password, or ``None`` when it is not stored."""

        return self._passwords.get(account_id)

    def delete_password(self, account_id: str) -> None:
        """Remove a password from this process-local session."""

        self._passwords.pop(account_id, None)

    def __repr__(self) -> str:
        """Avoid exposing stored credentials or account identifiers."""

        return "SessionCredentialStore()"