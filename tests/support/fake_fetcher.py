"""Deterministic in-memory implementation of the remote fetcher port."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime

from mail_dock.domain.errors import PermanentError, TransientError
from mail_dock.domain.fetcher import (
    BaseMailFetcher,
    CancelToken,
    RemoteFolder,
    RemoteMessageRef,
)


@dataclass(frozen=True)
class FakeMessage:
    """One message definition used to seed a :class:`FakeFetcher`."""

    ref: RemoteMessageRef
    raw: bytes


class FakeFetcher(BaseMailFetcher):
    """A controllable fetcher for sync tests, with no network or filesystem I/O."""

    def __init__(
        self,
        folders: Iterable[RemoteFolder] = (),
        messages: Mapping[str, Iterable[FakeMessage | RemoteMessageRef]] | None = None,
        eml_bytes: Mapping[tuple[str, int], bytes] | None = None,
        *,
        uidvalidities: Mapping[str, int] | None = None,
        transient_failures: Mapping[tuple[str, int], int] | None = None,
        permanent_failures: Iterable[tuple[str, int]] = (),
    ) -> None:
        self._folders = {folder.raw_name: folder for folder in folders}
        self._messages: dict[tuple[str, int], FakeMessage] = {}
        self._uidvalidities = dict(uidvalidities or {})
        self._transient_failures = dict(transient_failures or {})
        self._permanent_failures = set(permanent_failures)
        self._connected = False

        for raw_name, definitions in (messages or {}).items():
            for definition in definitions:
                if isinstance(definition, FakeMessage):
                    self._messages[(raw_name, definition.ref.uid)] = definition
                else:
                    self.add_message(raw_name, definition.uid, b"", ref=definition)
        for (raw_name, uid), raw in (eml_bytes or {}).items():
            existing = self._messages.get((raw_name, uid))
            if existing is None:
                self.add_message(raw_name, uid, raw)
            else:
                self._messages[(raw_name, uid)] = FakeMessage(existing.ref, raw)

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def messages(self) -> dict[tuple[str, int], FakeMessage]:
        return dict(self._messages)

    def add_folder(self, folder: RemoteFolder) -> None:
        self._folders[folder.raw_name] = folder
        if folder.uidvalidity is not None:
            self._uidvalidities[folder.raw_name] = folder.uidvalidity

    def add_message(
        self,
        raw_name: str,
        uid: int,
        raw: bytes,
        *,
        message_id: str | None = None,
        internal_date: datetime | None = None,
        size_bytes: int | None = None,
        flags: tuple[str, ...] = (),
        ref: RemoteMessageRef | None = None,
    ) -> None:
        message_ref = ref or RemoteMessageRef(
            uid=uid,
            message_id=message_id,
            internal_date=internal_date,
            size_bytes=len(raw) if size_bytes is None else size_bytes,
            flags=flags,
        )
        self._messages[(raw_name, uid)] = FakeMessage(message_ref, raw)
        if raw_name not in self._folders:
            self.add_folder(RemoteFolder(raw_name, raw_name, self._uidvalidities.get(raw_name, 1)))

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def list_folders(self) -> list[RemoteFolder]:
        return list(self._folders.values())

    def select_folder(self, raw_name: str) -> int:
        if raw_name not in self._folders:
            raise PermanentError(f"unknown folder: {raw_name}")
        return self._uidvalidities.get(raw_name, self._folders[raw_name].uidvalidity or 1)

    def iter_message_refs(
        self,
        raw_name: str,
        *,
        min_uid: int = 1,
        max_uid: int | None = None,
        descending: bool = True,
        cancel: CancelToken | None = None,
    ) -> Iterator[RemoteMessageRef]:
        token = cancel or CancelToken()
        uids = [
            uid
            for folder, uid in self._messages
            if folder == raw_name and uid >= min_uid and (max_uid is None or uid <= max_uid)
        ]
        for uid in sorted(uids, reverse=descending):
            token.raise_if_cancelled()
            yield self._messages[(raw_name, uid)].ref

    def get_max_uid(self, raw_name: str) -> int:
        return max((uid for folder, uid in self._messages if folder == raw_name), default=0)

    def list_existing_uids(self, raw_name: str) -> set[int]:
        return {uid for folder, uid in self._messages if folder == raw_name}

    def _raise_injected_failure(self, raw_name: str, uid: int) -> None:
        key = (raw_name, uid)
        if key in self._permanent_failures:
            raise PermanentError(f"injected permanent failure for {raw_name}:{uid}")
        remaining = self._transient_failures.get(key, 0)
        if remaining > 0:
            self._transient_failures[key] = remaining - 1
            raise TransientError(f"injected transient failure for {raw_name}:{uid}")

    def _message(self, raw_name: str, uid: int) -> FakeMessage:
        self._raise_injected_failure(raw_name, uid)
        try:
            return self._messages[(raw_name, uid)]
        except KeyError as exc:
            raise PermanentError(f"unknown message: {raw_name}:{uid}") from exc

    def download_eml_bytes(self, raw_name: str, uid: int) -> bytes:
        return self._message(raw_name, uid).raw

    def download_eml_headers(self, raw_name: str, uid: int) -> bytes:
        raw = self._message(raw_name, uid).raw
        separator = b"\r\n\r\n" if b"\r\n\r\n" in raw else b"\n\n"
        head, _, _ = raw.partition(separator)
        return head + separator

    def delete_remote_message(self, raw_name: str, uid: int, *, mode: str = "trash") -> None:
        if mode not in {"trash", "expunge"}:
            raise PermanentError(f"unsupported delete mode: {mode}")
        try:
            del self._messages[(raw_name, uid)]
        except KeyError as exc:
            raise PermanentError(f"unknown message: {raw_name}:{uid}") from exc