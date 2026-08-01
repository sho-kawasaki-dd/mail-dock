from __future__ import annotations

import pytest

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
from mail_dock.presentation.errors import ERROR_PRESENTATIONS, present_error

ERROR_TYPES = (
    MailDockError,
    SearchQueryError,
    ConfigError,
    ConfigVersionTooNewError,
    StorageError,
    StorageDetachedError,
    StorageForeignRootError,
    StorageRootMissingError,
    StorageLockedError,
    InsufficientSpaceError,
    DatabaseError,
    MigrationError,
    SchemaVersionTooNewError,
    FetchError,
    AuthenticationError,
    TransientError,
    PermanentError,
    UidValidityChanged,
    OversizeError,
    ManifestCorruptError,
    CredentialStoreError,
    OperationCancelledError,
)


@pytest.mark.parametrize("error_type", ERROR_TYPES)
def test_every_domain_error_has_a_presentation(error_type: type[MailDockError]) -> None:
    assert error_type in ERROR_PRESENTATIONS
    assert present_error(error_type("internal detail")).message


def test_known_error_does_not_expose_exception_detail() -> None:
    presentation = present_error(StorageDetachedError("/secret/storage/path"))

    assert presentation.message == strings.ERROR_STORAGE_DETACHED
    assert "/secret/storage/path" not in presentation.message
    assert presentation.recovery_action == strings.RECOVERY_RECONNECT_STORAGE


def test_unknown_exception_uses_generic_message_and_log_action() -> None:
    presentation = present_error(RuntimeError("private implementation detail"))

    assert presentation.message == strings.ERROR_UNKNOWN
    assert presentation.show_log_folder is True
    assert "private implementation detail" not in presentation.message
