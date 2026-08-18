from __future__ import annotations

import builtins
import imaplib
from collections.abc import Iterator
from typing import ClassVar, cast

import pytest

from mail_dock.domain.errors import AuthenticationError, OperationCancelledError
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
    flags: ClassVar[dict[int, tuple[str, ...]]] = {}
    search_uids: ClassVar[list[int]] = [1, 2, 3]
    capability_response: ClassVar[bytes] = b"CAPABILITY IMAP4rev1 MOVE UIDPLUS SPECIAL-USE"
    highest_modseq: ClassVar[int | None] = None
    nomodseq: ClassVar[bool] = False
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
        return "OK", [self.capability_response]

    def enable(self, capability: str) -> tuple[str, builtins.list[bytes]]:
        self.commands.append(("ENABLE", (capability,)))
        return "OK", [capability.encode("ascii")]

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
        if code == "HIGHESTMODSEQ":
            if self.nomodseq:
                return "NOMODSEQ", [b"[NOMODSEQ]"]
            if self.highest_modseq is None:
                return "", []
            return "HIGHESTMODSEQ", [str(self.highest_modseq).encode("ascii")]
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
            requested_uids = (
                self.search_uids
                if uid_set == "1:*"
                else [int(value) for value in uid_set.split(",")]
            )
            for uid in requested_uids:
                if request == "(UID FLAGS)":
                    flags = " ".join(self.flags.get(uid, ()))
                    items.append(f"* {uid} FETCH (UID {uid} FLAGS ({flags}))".encode("ascii"))
                    continue
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
    FakeImap.flags = {}
    FakeImap.capability_response = b"CAPABILITY IMAP4rev1 MOVE UIDPLUS SPECIAL-USE"
    FakeImap.highest_modseq = None
    FakeImap.nomodseq = False
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


def test_iter_flags_uses_flags_only_and_500_uid_chunks(fake_imap: None) -> None:
    FakeImap.search_uids = list(range(1, 502))
    FakeImap.flags = {uid: (r"\Seen",) for uid in FakeImap.search_uids}
    fetcher = OnamaeImapFetcher("imap.example.test", "user", "password")
    fetcher.connect()

    refs = list(fetcher.iter_flags("INBOX", FakeImap.search_uids))

    assert len(refs) == 501
    assert refs[0].uid == 1
    assert refs[0].flags == (r"\Seen",)
    fetch_commands = [
        command for command in FakeImap.instances[0].commands if command[0] == "FETCH"
    ]
    assert len(fetch_commands) == 2
    assert fetch_commands[0][1][1] == "(UID FLAGS)"
    assert len(cast(str, fetch_commands[0][1][0]).split(",")) == 500


def test_iter_flags_since_uses_condstore_and_reads_highest_modseq(fake_imap: None) -> None:
    FakeImap.capability_response = b"CAPABILITY IMAP4rev1 CONDSTORE"
    FakeImap.highest_modseq = 42
    FakeImap.flags = {1: (r"\Flagged",)}
    fetcher = OnamaeImapFetcher("imap.example.test", "user", "password")
    fetcher.connect()

    assert fetcher.select_folder("INBOX") == 123
    refs = list(fetcher.iter_flags_since("INBOX", 41))

    assert fetcher.get_highest_modseq() == 42
    assert refs[0].flags == (r"\Flagged",)
    assert ("ENABLE", ("CONDSTORE",)) in FakeImap.instances[0].commands
    assert ("FETCH", ("1:*", "(UID FLAGS)", "(CHANGEDSINCE 41)")) in (
        FakeImap.instances[0].commands
    )


def test_select_folder_reports_nomodseq_as_unavailable(fake_imap: None) -> None:
    FakeImap.capability_response = b"CAPABILITY IMAP4rev1 CONDSTORE"
    FakeImap.nomodseq = True
    fetcher = OnamaeImapFetcher("imap.example.test", "user", "password")
    fetcher.connect()

    fetcher.select_folder("INBOX")

    assert fetcher.get_highest_modseq() is None


def test_iter_flags_honors_cancellation_before_fetch(fake_imap: None) -> None:
    token = CancelToken()
    token.cancel()
    fetcher = OnamaeImapFetcher("imap.example.test", "user", "password")
    fetcher.connect()

    with pytest.raises(OperationCancelledError):
        list(fetcher.iter_flags("INBOX", [1], cancel=token))


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
