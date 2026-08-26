"""IMAP over SSL fetcher for Onamae and compatible IMAP servers.

The fetcher owns exactly one live IMAP connection. It translates all protocol
operations at the infrastructure boundary; retry and backoff belong to the
use-case layer and are intentionally absent here.
"""

from __future__ import annotations

import imaplib
import re
import ssl
from collections.abc import Iterable, Iterator
from contextlib import suppress
from typing import cast

from mail_dock.domain.errors import AuthenticationError, PermanentError
from mail_dock.domain.fetcher import (
    BaseMailFetcher,
    CancelToken,
    RemoteFolder,
    RemoteMessageRef,
)
from mail_dock.infrastructure.fetchers.imap_common import (
    parse_fetch_response,
    parse_list_responses,
    wrap_imap_errors,
)

_FETCH_CHUNK_SIZE = 500
_UIDVALIDITY_PATTERN = re.compile(r"\bUIDVALIDITY\s+(\d+)", re.IGNORECASE)
_HIGHEST_MODSEQ_PATTERN = re.compile(r"\bHIGHESTMODSEQ\s+(\d+)", re.IGNORECASE)
_NOMODSEQ_PATTERN = re.compile(r"\bNOMODSEQ\b", re.IGNORECASE)
_TRASH_CANDIDATES = (
    "Trash",
    "ゴミ箱",
    "Deleted Items",
    "Deleted Messages",
    "INBOX.Trash",
)


class OnamaeImapFetcher(BaseMailFetcher):
    """Fetch mail through one authenticated IMAP4_SSL connection.

    The default settings match the Onamae IMAPS endpoint. This class does not
    retry commands; retry and backoff are centralized in the use-case layer.

    Real-server checks still required for Onamae are the hierarchy delimiter,
    modified UTF-7 folder names, the concurrent-connection limit, timeout
    behavior, and support for MOVE, UIDPLUS, and SPECIAL-USE.
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        port: int = 993,
        timeout: float = 30.0,
        read_timeout: float | None = None,
        remote_trash_folder: str | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        if not host:
            raise ValueError("host must not be empty")
        if not username:
            raise ValueError("username must not be empty")
        if port <= 0:
            raise ValueError("port must be positive")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if read_timeout is not None and read_timeout <= 0:
            raise ValueError("read_timeout must be positive")
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._timeout = timeout
        self._read_timeout = read_timeout if read_timeout is not None else timeout
        self._remote_trash_folder = remote_trash_folder
        self._ssl_context = ssl_context
        self._connection: imaplib.IMAP4_SSL | None = None
        self._capabilities: frozenset[str] = frozenset()
        self._highest_modseq: int | None = None

    @property
    def capabilities(self) -> frozenset[str]:
        """Return the server capabilities recorded during ``connect``."""

        return self._capabilities

    def connect(self) -> None:
        """Open one TLS connection and authenticate it with LOGIN."""

        if self._connection is not None:
            return

        connection: imaplib.IMAP4_SSL | None = None
        try:
            with wrap_imap_errors("IMAP connect"):
                if self._ssl_context is None:
                    connection = imaplib.IMAP4_SSL(
                        self._host,
                        self._port,
                        timeout=self._timeout,
                    )
                else:
                    connection = imaplib.IMAP4_SSL(
                        self._host,
                        self._port,
                        ssl_context=self._ssl_context,
                        timeout=self._timeout,
                    )
                connection.sock.settimeout(self._read_timeout)
                status, data = connection.login(self._username, self._password)
                self._ensure_ok(status, data, "LOGIN")
                capability_status, capability_data = connection.capability()
                self._ensure_ok(capability_status, capability_data, "CAPABILITY")
                self._capabilities = self._parse_capabilities(capability_data)
                if "CONDSTORE" in self._capabilities:
                    enable_status, enable_data = connection.enable("CONDSTORE")
                    self._ensure_ok(enable_status, enable_data, "ENABLE CONDSTORE")
            self._connection = connection
        except BaseException:
            if connection is not None:
                with suppress(Exception):
                    connection.logout()
            raise

    def disconnect(self) -> None:
        """Close the single live connection, if one is open."""

        connection = self._connection
        self._connection = None
        self._capabilities = frozenset()
        self._highest_modseq = None
        if connection is None:
            return
        with wrap_imap_errors("IMAP logout"):
            connection.logout()

    def list_folders(self) -> list[RemoteFolder]:
        """List folders and decode their modified UTF-7 display names."""

        connection = self._require_connection()
        with wrap_imap_errors("LIST folders"):
            status, data = connection.list('""', "*")
            self._ensure_ok(status, data, "LIST")
        return parse_list_responses(
            item for item in self._response_items(data) if isinstance(item, (bytes, str))
        )

    def find_trash_folder(self) -> RemoteFolder | None:
        """Resolve the trash folder using SPECIAL-USE, candidates, then configuration."""

        folders = self.list_folders()
        for folder in folders:
            if any(attribute.casefold() == r"\trash" for attribute in folder.special_use):
                return folder

        normalized = {
            value.casefold(): folder
            for folder in folders
            for value in (folder.raw_name, folder.display_name)
        }
        for candidate in _TRASH_CANDIDATES:
            candidate_folder = normalized.get(candidate.casefold())
            if candidate_folder is not None:
                return candidate_folder

        configured = self._remote_trash_folder
        if configured:
            return normalized.get(configured.casefold()) or RemoteFolder(configured, configured)
        return None

    def supports_uid_expunge(self) -> bool:
        """Return whether this connection can expunge only the requested UID."""

        return "UIDPLUS" in self._capabilities

    def select_folder(self, raw_name: str) -> int:
        """Select a folder read-only and return its current UIDVALIDITY."""

        if not raw_name:
            raise ValueError("raw_name must not be empty")
        connection = self._require_connection()
        self._highest_modseq = None
        with wrap_imap_errors(f"SELECT {raw_name}"):
            status, data = connection.select(raw_name, readonly=True)
            self._ensure_ok(status, data, "SELECT")
            response_type, response_data = connection.response("UIDVALIDITY")
            highest_type: object = ""
            highest_data: object = []
            if "CONDSTORE" in self._capabilities:
                highest_type, highest_data = connection.response("HIGHESTMODSEQ")
        if str(response_type).upper() != "UIDVALIDITY":
            raise PermanentError("SELECT response has no UIDVALIDITY")
        uidvalidity = self._first_number(response_data)
        if uidvalidity is None:
            uidvalidity = self._first_number(data)
        if uidvalidity is None:
            raise PermanentError("SELECT response has no UIDVALIDITY")
        if "CONDSTORE" in self._capabilities:
            highest_type_text = self._response_text(highest_type).upper()
            if (
                highest_type_text == "NOMODSEQ"
                or self._contains_nomodseq(data)
                or self._contains_nomodseq(highest_data)
            ):
                self._highest_modseq = None
            elif highest_type_text == "HIGHESTMODSEQ":
                self._highest_modseq = self._first_modseq(highest_data)
            else:
                self._highest_modseq = self._first_modseq(data, allow_plain=False)
        return uidvalidity

    def get_highest_modseq(self) -> int | None:
        """Return the HIGHESTMODSEQ recorded by the last folder selection."""

        return self._highest_modseq

    def iter_message_refs(
        self,
        raw_name: str,
        *,
        min_uid: int = 1,
        max_uid: int | None = None,
        descending: bool = True,
        cancel: CancelToken | None = None,
    ) -> Iterator[RemoteMessageRef]:
        """Yield 500-message metadata FETCH chunks in the requested UID order."""

        if min_uid < 1:
            raise ValueError("min_uid must be at least 1")
        if max_uid is not None and max_uid < 0:
            raise ValueError("max_uid must not be negative")
        token = cancel if cancel is not None else CancelToken()
        self.select_folder(raw_name)
        if max_uid == 0 or (max_uid is not None and max_uid < min_uid):
            return

        token.raise_if_cancelled()
        upper_bound = str(max_uid) if max_uid is not None else "*"
        search_data = self._uid_command("SEARCH", None, f"UID {min_uid}:{upper_bound}")
        uids = self._search_uids(search_data)
        uids.sort(reverse=descending)
        for offset in range(0, len(uids), _FETCH_CHUNK_SIZE):
            token.raise_if_cancelled()
            chunk = uids[offset : offset + _FETCH_CHUNK_SIZE]
            data = self._uid_command(
                "FETCH",
                ",".join(str(uid) for uid in chunk),
                "(UID INTERNALDATE RFC822.SIZE FLAGS BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])",
            )
            for item in data:
                if not isinstance(item, tuple) or len(item) != 2:
                    continue
                metadata, literal = item
                if not isinstance(metadata, bytes) or not isinstance(literal, bytes):
                    continue
                yield parse_fetch_response((metadata, literal))
            token.raise_if_cancelled()

    def iter_flags(
        self,
        raw_name: str,
        uids: Iterable[int],
        *,
        cancel: CancelToken | None = None,
    ) -> Iterator[RemoteMessageRef]:
        """Yield FLAGS-only metadata for the supplied UIDs in chunks."""

        requested_uids = list(uids)
        if any(uid < 1 for uid in requested_uids):
            raise ValueError("uids must contain only positive integers")
        token = cancel if cancel is not None else CancelToken()
        self.select_folder(raw_name)
        for offset in range(0, len(requested_uids), _FETCH_CHUNK_SIZE):
            token.raise_if_cancelled()
            chunk = requested_uids[offset : offset + _FETCH_CHUNK_SIZE]
            data = self._uid_command(
                "FETCH",
                ",".join(str(uid) for uid in chunk),
                "(UID FLAGS)",
            )
            for item in data:
                if not isinstance(item, (bytes, tuple, list)):
                    continue
                yield parse_fetch_response(item)
            token.raise_if_cancelled()

    def iter_flags_since(
        self,
        raw_name: str,
        modseq: int,
        *,
        cancel: CancelToken | None = None,
    ) -> Iterator[RemoteMessageRef]:
        """Yield FLAGS-only metadata changed after the supplied MODSEQ."""

        if modseq <= 0:
            raise ValueError("modseq must be positive")
        token = cancel if cancel is not None else CancelToken()
        self.select_folder(raw_name)
        token.raise_if_cancelled()
        data = self._uid_command(
            "FETCH",
            "1:*",
            "(UID FLAGS)",
            f"(CHANGEDSINCE {modseq})",
        )
        for item in data:
            if not isinstance(item, (bytes, tuple, list)):
                continue
            yield parse_fetch_response(item)
        token.raise_if_cancelled()

    def get_max_uid(self, raw_name: str) -> int:
        """Return the largest UID without fetching message metadata."""

        self.select_folder(raw_name)
        return max(self._search_uids(self._uid_command("SEARCH", None, "ALL")), default=0)

    def list_existing_uids(self, raw_name: str) -> set[int]:
        """Return the current UID set using the lightweight SEARCH command."""

        self.select_folder(raw_name)
        return set(self._search_uids(self._uid_command("SEARCH", None, "ALL")))

    def download_eml_bytes(self, raw_name: str, uid: int) -> bytes:
        """Download one complete message without setting ``\\Seen``."""

        self.select_folder(raw_name)
        data = self._uid_command("FETCH", str(uid), "(BODY.PEEK[])")
        return self._literal_from_fetch(data, "message")

    def download_eml_headers(self, raw_name: str, uid: int) -> bytes:
        """Download only message headers without setting ``\\Seen``."""

        self.select_folder(raw_name)
        data = self._uid_command("FETCH", str(uid), "(BODY.PEEK[HEADER])")
        return self._literal_from_fetch(data, "message headers")

    def delete_remote_message(self, raw_name: str, uid: int, *, mode: str = "trash") -> None:
        """Move or expunge one message.

        Phase 4 callers must perform their safety checks before invoking this
        low-level provider operation. In particular, ``expunge`` is rejected
        unless UIDPLUS is advertised; a folder-wide EXPUNGE is never used as a
        fallback.
        """

        if mode not in {"trash", "expunge"}:
            raise ValueError("mode must be 'trash' or 'expunge'")
        connection = self._require_connection()
        with wrap_imap_errors(f"SELECT {raw_name} for deletion"):
            status, data = connection.select(raw_name)
            self._ensure_ok(status, data, "SELECT")
        if mode == "trash":
            trash_folder = self.find_trash_folder()
            if trash_folder is None:
                raise PermanentError("could not identify the remote trash folder")
            if "MOVE" in self._capabilities:
                self._uid_command("MOVE", str(uid), trash_folder.raw_name)
                return
            if not self.supports_uid_expunge():
                raise PermanentError(
                    "UID EXPUNGE is required when MOVE is not supported by this IMAP server"
                )
            self._uid_command("COPY", str(uid), trash_folder.raw_name)
        elif not self.supports_uid_expunge():
            raise PermanentError("UID EXPUNGE is not supported by this IMAP server")
        self._uid_command("STORE", str(uid), "+FLAGS.SILENT", r"(\Deleted)")
        self._uid_command("EXPUNGE", str(uid))

    def _require_connection(self) -> imaplib.IMAP4_SSL:
        if self._connection is None:
            raise PermanentError("IMAP connection is not open")
        return self._connection

    def _uid_command(self, command: str, *args: object) -> list[object]:
        connection = self._require_connection()
        with wrap_imap_errors(f"UID {command}"):
            imap_args = cast(tuple[str, ...], args)
            status, data = connection.uid(command, *imap_args)
            self._ensure_ok(status, data, f"UID {command}")
        return self._response_items(data)

    @staticmethod
    def _ensure_ok(status: object, data: object, operation: str) -> None:
        status_text = (
            status.decode("ascii", errors="replace") if isinstance(status, bytes) else str(status)
        )
        if status_text.upper() == "OK":
            return
        response_text = " ".join(
            item.decode("utf-8", errors="replace") if isinstance(item, bytes) else str(item)
            for item in OnamaeImapFetcher._response_items(data)
        )
        message = f"{operation} failed: {response_text[:200]}"
        if "AUTHENTICATIONFAILED" in message.upper():
            raise AuthenticationError(message)
        raise PermanentError(message)

    @staticmethod
    def _response_items(data: object) -> list[object]:
        if data is None:
            return []
        if isinstance(data, list):
            return data
        if isinstance(data, tuple):
            return list(data)
        return [data]

    @staticmethod
    def _parse_capabilities(data: object) -> frozenset[str]:
        tokens: set[str] = set()
        for item in OnamaeImapFetcher._response_items(data):
            text = item.decode("ascii", errors="replace") if isinstance(item, bytes) else str(item)
            if text.upper().startswith("CAPABILITY "):
                text = text.split(None, 1)[1]
            tokens.update(token.upper() for token in text.split())
        return frozenset(tokens)

    @staticmethod
    def _first_number(data: object) -> int | None:
        for item in OnamaeImapFetcher._response_items(data):
            text = item.decode("ascii", errors="replace") if isinstance(item, bytes) else str(item)
            match = _UIDVALIDITY_PATTERN.search(text)
            if match is not None:
                return int(match.group(1))
            if text.strip().isdigit():
                return int(text.strip())
        return None

    @staticmethod
    def _first_modseq(data: object, *, allow_plain: bool = True) -> int | None:
        items = OnamaeImapFetcher._response_items(data)
        for item in items:
            text = item.decode("ascii", errors="replace") if isinstance(item, bytes) else str(item)
            match = _HIGHEST_MODSEQ_PATTERN.search(text)
            if match is not None:
                return int(match.group(1))
        if allow_plain:
            for item in items:
                text = (
                    item.decode("ascii", errors="replace") if isinstance(item, bytes) else str(item)
                )
                if text.strip().isdigit():
                    return int(text.strip())
        return None

    @staticmethod
    def _response_text(value: object) -> str:
        if isinstance(value, bytes):
            return value.decode("ascii", errors="replace")
        return str(value)

    @staticmethod
    def _contains_nomodseq(data: object) -> bool:
        return any(
            _NOMODSEQ_PATTERN.search(
                item.decode("ascii", errors="replace") if isinstance(item, bytes) else str(item)
            )
            for item in OnamaeImapFetcher._response_items(data)
        )

    @staticmethod
    def _search_uids(data: object) -> list[int]:
        values: list[int] = []
        for item in OnamaeImapFetcher._response_items(data):
            text = item.decode("ascii", errors="replace") if isinstance(item, bytes) else str(item)
            values.extend(int(value) for value in text.split() if value.isdigit())
        return values

    @staticmethod
    def _literal_from_fetch(data: list[object], description: str) -> bytes:
        for item in data:
            if isinstance(item, tuple) and len(item) == 2:
                literal = item[1]
                if isinstance(literal, bytes):
                    return literal
        raise PermanentError(f"IMAP FETCH returned no {description}")
