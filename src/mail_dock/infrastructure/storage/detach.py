"""Classify storage I/O failures before they cross the infrastructure boundary.

This module is the only gate that prevents ``winerror`` values and SQLite
error codes from leaking to upper layers. Callers must never log or inspect
those infrastructure-specific details outside this module.
"""

import errno
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from mail_dock.domain.errors import StorageDetachedError

_DETACH_WINERRORS = frozenset({6, 21, 55, 433, 995, 1117, 1167})
_DETACH_ERRNOS = frozenset(
	{errno.EIO, errno.ENXIO, errno.ENODEV, getattr(errno, "ESTALE", 116)}
)


def classify_os_error(error: OSError) -> Exception:
	"""Convert OS errors that indicate detached storage to a domain error."""

	winerror = getattr(error, "winerror", None)
	if winerror in _DETACH_WINERRORS or error.errno in _DETACH_ERRNOS:
		return StorageDetachedError(str(error))
	return error


def classify_sqlite_error(error: sqlite3.Error) -> Exception:
	"""Convert SQLite I/O failures that indicate detached storage."""

	error_name = getattr(error, "sqlite_errorname", "")
	if (
		isinstance(error_name, str)
		and (
			error_name.startswith("SQLITE_IOERR")
			or error_name in {"SQLITE_READONLY_DBMOVED", "SQLITE_CANTOPEN"}
		)
	):
		return StorageDetachedError(str(error))
	return error


@contextmanager
def storage_io() -> Iterator[None]:
	"""Classify storage exceptions raised by the enclosed I/O operation."""

	try:
		yield
	except OSError as error:
		classified = classify_os_error(error)
		if classified is error:
			raise
		raise classified from error
	except sqlite3.Error as error:
		classified = classify_sqlite_error(error)
		if classified is error:
			raise
		raise classified from error
