"""Keyring-backed credential storage for mail-dock accounts."""

from __future__ import annotations

import base64
import json
from enum import StrEnum

import keyring
from keyring.errors import PasswordDeleteError

from mail_dock.domain.errors import CredentialStoreError
from mail_dock.domain.ports import MANAGED_SECRET_NAMES, BaseCredentialStore

_SERVICE_NAME = "mail-dock"
# An allowlist avoids silently trusting a newly added plaintext backend.
_ALLOWED_BACKENDS = frozenset(
    {
        "keyring.backends.Windows.WinVaultKeyring",
        "keyring.backends.macOS.Keyring",
        "keyring.backends.SecretService.Keyring",
        "keyring.backends.kwallet.DBusKeyring",
    }
)


class KeyringBackendStatus(StrEnum):
    """Availability of the configured keyring backend."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"


def _backend_name(backend: object) -> str:
    backend_type = type(backend)
    return f"{backend_type.__module__}.{backend_type.__qualname__}"


def detect_backend() -> KeyringBackendStatus:
    """Return whether the configured backend is in the supported allowlist."""

    try:
        backend = keyring.get_keyring()
    except Exception:
        return KeyringBackendStatus.UNAVAILABLE
    if _backend_name(backend) in _ALLOWED_BACKENDS:
        return KeyringBackendStatus.SUPPORTED
    return KeyringBackendStatus.UNSUPPORTED


def backend_name() -> str:
    """Return the configured backend's qualified type name for display."""

    try:
        return _backend_name(keyring.get_keyring())
    except Exception:
        return KeyringBackendStatus.UNAVAILABLE.value


def _ensure_supported_backend() -> None:
    status = detect_backend()
    if status is not KeyringBackendStatus.SUPPORTED:
        raise CredentialStoreError(
            f"Keyring backend is {status.value}; credentials will not be stored"
        )


class KeyringCredentialStore(BaseCredentialStore):
    """Store passwords using the operating system's configured keyring."""

    @staticmethod
    def _username(account_id: str, name: str) -> str:
        encoded = base64.urlsafe_b64encode(
            json.dumps([account_id, name], ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        ).decode("ascii")
        return f"mail-dock-secret:{encoded}"

    def set_password(self, account_id: str, password: str) -> None:
        """Store a password without exposing it to the database or logs."""

        try:
            _ensure_supported_backend()
            keyring.set_password(_SERVICE_NAME, account_id, password)
        except Exception as error:
            if isinstance(error, CredentialStoreError):
                raise
            raise CredentialStoreError("Could not store account credentials") from error

    def get_password(self, account_id: str) -> str | None:
        """Return a stored password, translating backend failures."""

        try:
            _ensure_supported_backend()
            return keyring.get_password(_SERVICE_NAME, account_id)
        except Exception as error:
            if isinstance(error, CredentialStoreError):
                raise
            raise CredentialStoreError("Could not read account credentials") from error

    def delete_password(self, account_id: str) -> None:
        """Delete a stored password, translating backend failures."""

        try:
            _ensure_supported_backend()
            keyring.delete_password(_SERVICE_NAME, account_id)
        except PasswordDeleteError:
            return
        except Exception as error:
            if isinstance(error, CredentialStoreError):
                raise
            raise CredentialStoreError("Could not delete account credentials") from error

    def set_secret(self, account_id: str, name: str, value: str) -> None:
        """Store one namespaced secret using the supported OS keyring."""

        try:
            _ensure_supported_backend()
            keyring.set_password(_SERVICE_NAME, self._username(account_id, name), value)
        except Exception as error:
            if isinstance(error, CredentialStoreError):
                raise
            raise CredentialStoreError("Could not store account secret") from error

    def get_secret(self, account_id: str, name: str) -> str | None:
        """Return one namespaced secret, translating backend failures."""

        try:
            _ensure_supported_backend()
            return keyring.get_password(_SERVICE_NAME, self._username(account_id, name))
        except Exception as error:
            if isinstance(error, CredentialStoreError):
                raise
            raise CredentialStoreError("Could not read account secret") from error

    def delete_secret(self, account_id: str, name: str) -> None:
        """Delete one namespaced secret; a missing key is an idempotent success."""

        try:
            _ensure_supported_backend()
            keyring.delete_password(_SERVICE_NAME, self._username(account_id, name))
        except PasswordDeleteError:
            return
        except Exception as error:
            if isinstance(error, CredentialStoreError):
                raise
            raise CredentialStoreError("Could not delete account secret") from error

    def delete_all_secrets(self, account_id: str) -> None:
        """Remove every secret name owned by mail-dock, including absent keys."""

        for name in MANAGED_SECRET_NAMES:
            self.delete_secret(account_id, name)
        self.delete_password(account_id)
