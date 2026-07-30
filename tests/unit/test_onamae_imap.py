from __future__ import annotations

import builtins
import imaplib
from collections.abc import Iterator
from typing import ClassVar, cast

import pytest

from mail_dock.domain.errors import AuthenticationError
from mail_dock.domain.fetcher import CancelToken
from mail_dock.infrastructure.fetchers.onamae_imap import OnamaeImapFetcher


class FakeSocket:
    def __init__(self) -> None:
        self.timeouts: list[float] = []

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)


class FakeImap:
    instances: ClassVar[list[FakeImap]] = []
    messages: ClassVar[dict[int, bytes]] = {}
    search_uids: ClassVar[list[int]] = [1, 2, 3]
    commands: list[tuple[str, tuple[object, ...]]]
    login_result: ClassVar[tuple[str, builtins.list[bytes]]] = ("OK", [b"LOGIN completed"])

    def __init__(self, host: str, port: int, *, timeout: float) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock = FakeSocket()
        self.commands = []
        self.uidvalidity = 123
        self.__class__.instances.append(self)

    def login(self, username: str, password: str) -> tuple[str, builtins.list[bytes]]:
        self.commands.append(("LOGIN", (username, password)))
        return self.login_result

    def capability(self) -> tuple[str, builtins.list[bytes]]:
        self.commands.append(("CAPABILITY", ()))
        return "OK", [b"CAPABILITY IMAP4rev1 MOVE UIDPLUS SPECIAL-USE"]

    def logout(self) -> tuple[str, list[bytes]]:
        self.commands.append(("LOGOUT", ()))
        return "BYE", [b"LOGOUT completed"]

    def list(self, reference: str, pattern: str) -> tuple[str, builtins.list[bytes]]:
        self.commands.append(("LIST", (reference, pattern)))
        return "OK", [b'* LIST (\\HasNoChildren \\Trash) "." "Trash"', b'* LIST () "." "INBOX"']

    def select(self, mailbox: str, *, readonly: bool = False) -> tuple[str, builtins.list[bytes]]:
        self.commands.append(("SELECT", (mailbox, readonly)))
        return "OK", [b"1"]

    def response(self, code: str) -> tuple[str, builtins.list[bytes]]:
        return code, [str(self.uidvalidity).encode("ascii")]

    def uid(self, command: str, *args: str) -> tuple[str, builtins.list[object]]:
        self.commands.append((command, args))
        if command == "SEARCH":
            query = args[-1]
            if query == "ALL":
                return "OK", [" ".join(str(uid) for uid in self.search_uids).encode("ascii")]
            return "OK", [" ".join(str(uid) for uid in self.search_uids).encode("ascii")]
        if command == "FETCH":
            uid_set = args[0]
            request = args[1]
            items: list[object] = []
            for uid in (int(value) for value in uid_set.split(",")):
                raw = self.messages.get(uid, f"message-{uid}".encode("ascii"))
                if "HEADER.FIELDS" in request:
                    literal = f"Message-ID: <{uid}@example.test>\r\n\r\n".encode("ascii")
                    metadata = (
                        f"* {uid} FETCH (UID {uid} INTERNALDATE "
                        f'"30-Jul-2026 12:34:56 +0000" RFC822.SIZE {len(raw)} '
                        f"FLAGS () BODY[HEADER] {{{len(literal)}}}"
                    ).encode("ascii")
                elif "HEADER" in request:
                    literal = b"Subject: oversized\r\n\r\n"
                    metadata = f"* {uid} FETCH (BODY[HEADER] {{{len(literal)}}}".encode("ascii")
                else:
                    literal = raw
                    metadata = f"* {uid} FETCH (BODY[] {{{len(literal)}}}".encode("ascii")
                items.append((metadata, literal))
            return "OK", items
        return "OK", [b"completed"]

    def expunge(self) -> tuple[str, builtins.list[bytes]]:
        self.commands.append(("EXPUNGE", ()))
        return "OK", [b"completed"]


@pytest.fixture
def fake_imap(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    FakeImap.instances.clear()
    FakeImap.search_uids = [1, 2, 3]
    FakeImap.login_result = ("OK", [b"LOGIN completed"])
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeImap)
    yield


def test_connect_reuses_one_connection_and_records_capabilities(fake_imap: None) -> None:
    fetcher = OnamaeImapFetcher(
        "imap.example.test",
        "user@example.test",
        "password",
        timeout=11.0,
        read_timeout=17.0,
    )

    fetcher.connect()
    fetcher.connect()

    assert len(FakeImap.instances) == 1
    connection = FakeImap.instances[0]
    assert connection.timeout == 11.0
    assert connection.sock.timeouts == [17.0]
    assert fetcher.capabilities == frozenset({"IMAP4REV1", "MOVE", "UIDPLUS", "SPECIAL-USE"})

    fetcher.disconnect()


def test_iter_refs_searches_in_descending_500_message_chunks(fake_imap: None) -> None:
    FakeImap.messages = {uid: f"message-{uid}".encode("ascii") for uid in range(1, 502)}
    FakeImap.search_uids = list(range(1, 502))
    fetcher = OnamaeImapFetcher("imap.example.test", "user", "password")
    fetcher.connect()

    refs = list(
        fetcher.iter_message_refs(
            "INBOX",
            min_uid=1,
            max_uid=501,
            descending=True,
            cancel=CancelToken(),
        )
    )

    assert [ref.uid for ref in refs] == list(range(501, 0, -1))
    fetch_commands = [
        command for command in FakeImap.instances[0].commands if command[0] == "FETCH"
    ]
    assert len(fetch_commands) == 2
    first_uid_set = cast(str, fetch_commands[0][1][0])
    second_uid_set = cast(str, fetch_commands[1][1][0])
    assert len(first_uid_set.split(",")) == 500
    assert first_uid_set.split(",")[0] == "501"
    assert second_uid_set.split(",") == ["1"]


def test_downloads_use_peek_and_select_returns_uidvalidity(fake_imap: None) -> None:
    fetcher = OnamaeImapFetcher("imap.example.test", "user", "password")
    fetcher.connect()

    assert fetcher.select_folder("INBOX") == 123
    assert fetcher.download_eml_bytes("INBOX", 1) == b"message-1"
    assert fetcher.download_eml_headers("INBOX", 1) == b"Subject: oversized\r\n\r\n"

    fetch_commands = [
        command for command in FakeImap.instances[0].commands if command[0] == "FETCH"
    ]
    assert all("PEEK" in str(command[1][1]) for command in fetch_commands)


def test_delete_uses_move_when_server_supports_it(fake_imap: None) -> None:
    fetcher = OnamaeImapFetcher("imap.example.test", "user", "password")
    fetcher.connect()

    fetcher.delete_remote_message("INBOX", 7, mode="trash")

    commands = FakeImap.instances[0].commands
    assert ("MOVE", ("7", "Trash")) in commands
    assert not any(command[0] == "STORE" for command in commands)


def test_authentication_failure_is_translated(fake_imap: None) -> None:
    FakeImap.login_result = ("NO", [b"[AUTHENTICATIONFAILED] invalid credentials"])
    fetcher = OnamaeImapFetcher("imap.example.test", "user", "password")

    with pytest.raises(AuthenticationError):
        fetcher.connect()
