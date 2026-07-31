"""Domain-level errors used across mail-dock.

Infrastructure code must wrap raw exceptions here before passing them to
upper layers. This keeps protocol, filesystem, and database details outside
the domain and use-case layers.
"""


class MailDockError(Exception):
    """Base class for all expected mail-dock errors."""


class SearchQueryError(MailDockError):
    """Raised when user input is invalid and cannot be executed as a query."""


class ConfigError(MailDockError):
    """Raised when application configuration is invalid or cannot be read."""


class ConfigVersionTooNewError(ConfigError):
    """Raised when configuration was written by a newer application version."""


class StorageError(MailDockError):
    """Base class for errors involving the local mail storage."""


class StorageDetachedError(StorageError):
    """Raised when a storage device becomes unavailable during an operation."""


class StorageForeignRootError(StorageError):
    """Raised when a storage path contains a marker for a different root."""


class StorageRootMissingError(StorageError):
    """Raised when the configured storage root cannot be found."""


class StorageLockedError(StorageError):
    """Raised when another mail-dock instance owns the storage lock."""


class InsufficientSpaceError(StorageError):
    """Raised when storage has less than the minimum required free space."""


class DatabaseError(MailDockError):
    """Base class for errors involving the metadata database."""


class MigrationError(DatabaseError):
    """Raised when applying a database migration fails or leaves invalid data."""


class SchemaVersionTooNewError(DatabaseError):
    """Raised when a database requires migrations unknown to this application."""


class FetchError(MailDockError):
    """Base class for errors raised while fetching mail from a remote source."""


class AuthenticationError(FetchError):
    """Raised when remote authentication is rejected and must not be retried."""


class TransientError(FetchError):
    """Raised for a temporary remote failure that may succeed on retry."""


class PermanentError(FetchError):
    """Raised for a remote failure that cannot be fixed by retrying."""


class UidValidityChanged(FetchError):  # noqa: N818
    """Control-flow signal indicating that a folder needs a new UID generation."""


class OversizeError(FetchError):
    """Raised when a message is larger than the configured download limit."""


class ManifestCorruptError(StorageError):
    """Raised when a non-tail manifest record cannot be recovered safely."""


class CredentialStoreError(MailDockError):
    """Raised when the operating-system credential store is unavailable."""


class OperationCancelledError(MailDockError):
    """Raised when a user or application requests cancellation of an operation."""
