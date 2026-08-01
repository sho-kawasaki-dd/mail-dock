"""Map domain failures to safe, actionable GUI error presentations.

This module deliberately has no Qt dependency. Views can use the returned
value to decide whether to offer a log-folder action without exposing an
exception message or traceback directly to the user.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, cast

from mail_dock.domain.errors import (
    AuthenticationError,
    ConfigError,
    ConfigVersionTooNewError,
    CredentialStoreError,
    DatabaseError,
    FetchError,
    InsufficientSpaceError,
    MailDockError,
    ManifestCorruptError,
    MigrationError,
    OperationCancelledError,
    OversizeError,
    PermanentError,
    SchemaVersionTooNewError,
    SearchQueryError,
    StorageDetachedError,
    StorageError,
    StorageForeignRootError,
    StorageLockedError,
    StorageRootMissingError,
    TransientError,
    UidValidityChanged,
)
from mail_dock.presentation import strings


@dataclass(frozen=True)
class ErrorPresentation:
    """User-facing text and recovery affordances for one domain failure."""

    message: str
    recovery_action: str | None = None
    show_log_folder: bool = False


def _presentation(
    message: str,
    *,
    recovery_action: str | None = None,
    show_log_folder: bool = False,
) -> ErrorPresentation:
    return ErrorPresentation(
        message=message,
        recovery_action=recovery_action,
        show_log_folder=show_log_folder,
    )


# Keep an entry for every MailDockError class, including base categories. This
# makes coverage of the error contract explicit as the hierarchy grows.
ERROR_PRESENTATIONS: Final = MappingProxyType(
    {
        MailDockError: _presentation(strings.ERROR_UNKNOWN, show_log_folder=True),
        SearchQueryError: _presentation(strings.SEARCH_INVALID_QUERY),
        ConfigError: _presentation(strings.ERROR_CONFIG, show_log_folder=True),
        ConfigVersionTooNewError: _presentation(
            strings.ERROR_CONFIG_VERSION,
            show_log_folder=True,
        ),
        StorageError: _presentation(strings.ERROR_STORAGE, show_log_folder=True),
        StorageDetachedError: _presentation(
            strings.ERROR_STORAGE_DETACHED,
            recovery_action=strings.RECOVERY_RECONNECT_STORAGE,
        ),
        StorageForeignRootError: _presentation(strings.ERROR_FOREIGN_ROOT),
        StorageRootMissingError: _presentation(
            strings.ERROR_STORAGE_ROOT_MISSING,
            recovery_action=strings.RECOVERY_SELECT_STORAGE,
        ),
        StorageLockedError: _presentation(strings.ERROR_STORAGE_LOCKED),
        InsufficientSpaceError: _presentation(
            strings.ERROR_INSUFFICIENT_SPACE,
            recovery_action=strings.RECOVERY_FREE_STORAGE,
        ),
        DatabaseError: _presentation(strings.ERROR_DATABASE, show_log_folder=True),
        MigrationError: _presentation(strings.ERROR_DATABASE, show_log_folder=True),
        SchemaVersionTooNewError: _presentation(strings.ERROR_DATABASE, show_log_folder=True),
        FetchError: _presentation(strings.ERROR_CONNECTION),
        AuthenticationError: _presentation(
            strings.ERROR_AUTHENTICATION,
            recovery_action=strings.RECOVERY_CHECK_CREDENTIALS,
        ),
        TransientError: _presentation(
            strings.ERROR_CONNECTION,
            recovery_action=strings.RECOVERY_RETRY,
        ),
        PermanentError: _presentation(strings.ERROR_CONNECTION, show_log_folder=True),
        UidValidityChanged: _presentation(strings.ERROR_SYNC_RESTART_REQUIRED),
        OversizeError: _presentation(strings.ERROR_OVERSIZE),
        ManifestCorruptError: _presentation(strings.ERROR_MANIFEST, show_log_folder=True),
        CredentialStoreError: _presentation(
            strings.ERROR_CREDENTIAL_STORE,
            recovery_action=strings.RECOVERY_CHECK_CREDENTIAL_STORE,
            show_log_folder=True,
        ),
        OperationCancelledError: _presentation(strings.ERROR_CANCELLED),
    }
)


def present_error(error: BaseException) -> ErrorPresentation:
    """Return a safe presentation for ``error`` without exposing its detail."""

    if isinstance(error, MailDockError):
        error_type: type[MailDockError] = type(error)
        presentation = ERROR_PRESENTATIONS.get(error_type)
        if presentation is not None:
            return presentation
        for error_class in error_type.__mro__[1:]:
            base_class = cast(type[MailDockError], error_class)
            presentation = ERROR_PRESENTATIONS.get(base_class)
            if presentation is not None:
                return presentation
    return ERROR_PRESENTATIONS[MailDockError]


def user_message(error: BaseException) -> str:
    """Return only the user-facing message for ``error``."""

    return present_error(error).message