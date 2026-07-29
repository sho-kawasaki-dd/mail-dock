"""Domain-level errors used across mail-dock.

Infrastructure code must wrap raw exceptions here before passing them to
upper layers. This keeps protocol, filesystem, and database details outside
the domain and use-case layers.
"""


class MailDockError(Exception):
	"""Base class for all expected mail-dock errors."""


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


# Phase 1 adds AuthenticationError, TransientError, and PermanentError here.
# Phase 4.5 adds archive-import-specific leaf errors here as needed.


class OperationCancelledError(MailDockError):
	"""Raised when a user or application requests cancellation of an operation."""
