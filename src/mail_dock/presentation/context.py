"""GUI composition context.

Views and view models receive this object rather than importing infrastructure
implementations directly. The resource owners remain in ``StorageSession``;
this context only exposes their active lifetime to the presentation layer.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import suppress
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from mail_dock import config
from mail_dock.domain.errors import ConfigError
from mail_dock.domain.fetcher import BaseMailFetcher
from mail_dock.domain.ports import (
    BaseCredentialStore,
    BaseEmlStorage,
    BaseManifestReader,
    BaseManifestWriter,
    BasePurgeStorage,
    JSONValue,
)
from mail_dock.domain.repository import MessageRecord
from mail_dock.domain.search import BaseSearchRepository
from mail_dock.infrastructure.database.message_repository import SqliteMessageRepository
from mail_dock.infrastructure.database.search_repository import SqliteSearchRepository
from mail_dock.infrastructure.fetchers.onamae_imap import OnamaeImapFetcher
from mail_dock.infrastructure.security.keyring_store import (
    KeyringBackendStatus,
    backend_name,
    detect_backend,
)
from mail_dock.infrastructure.storage.capabilities import (
    capability_level,
    probe_capabilities,
    storage_fingerprint,
)
from mail_dock.infrastructure.storage.eml_storage import EmlStorage
from mail_dock.infrastructure.storage.manifest import ManifestReader, ManifestWriter
from mail_dock.usecases.register_account import load_credentials

if TYPE_CHECKING:
    from mail_dock.__main__ import StorageSession


MessageRendererFactory = Callable[[], Any]
HtmlSanitizerFactory = Callable[..., str]


class _CombinedManifestReader(BaseManifestReader):
    """Read account manifests through one provider-independent reader port."""

    def __init__(self, readers: tuple[BaseManifestReader, ...]) -> None:
        self._readers = readers

    def read_all_events(self) -> Iterator[Mapping[str, JSONValue]]:
        for reader in self._readers:
            yield from reader.read_all_events()

    def read_last_checkpoint(self) -> Mapping[str, JSONValue] | None:
        checkpoints = [
            checkpoint
            for reader in self._readers
            if (checkpoint := reader.read_last_checkpoint()) is not None
        ]
        return max(
            checkpoints,
            key=lambda checkpoint: str(checkpoint.get("timestamp", "")),
            default=None,
        )

    def read_events_since_checkpoint(self) -> Iterator[Mapping[str, JSONValue]]:
        for reader in self._readers:
            yield from reader.read_events_since_checkpoint()

    def read_incomplete_intents(self) -> Iterator[Mapping[str, JSONValue]]:
        for reader in self._readers:
            yield from reader.read_incomplete_intents()


class AppContext:
    """Composition root for the GUI after a storage session has started.

    Views, view models, and presentation models must reach infrastructure
    implementations through this object.  The context borrows the lock and
    connection manager from ``StorageSession``; it never closes either one.
    Every factory creates its object in the thread that calls it, so no
    SQLite connection or live IMAP connection is shared between workers.
    """

    def __init__(
        self,
        session: StorageSession,
        settings: config.AppConfig,
        *,
        renderer_factory: MessageRendererFactory | None = None,
    ) -> None:
        self.storage_root = session.root
        self.root_uuid = session.root_uuid
        self.settings = settings
        self.storage_lock = session.storage_lock
        self.connection_manager = session.connection_manager
        self.capabilities = (
            session.capabilities.as_dict() if session.capabilities is not None else None
        )
        self.capability_level = (
            session.capability_level.value if session.capability_level is not None else None
        )
        self.encryption_declaration = session.encryption_declaration
        self.credential_storage = session.credential_storage_mode
        self.credential_backend_name = backend_name()
        self.keyring_supported = detect_backend() is KeyringBackendStatus.SUPPORTED
        self._session = session
        self._renderer_factory = renderer_factory
        self.storage_root_switch_handler: Callable[[Path], None] | None = None
        self.storage_setup_handler: Callable[[Path | None], None] | None = None
        self.storage_detach_handler: Callable[[], None] | None = None
        self.window_created_handler: Callable[[Any], None] | None = None

    @property
    def database_path(self) -> Path:
        """Return the active metadata database path."""

        return self.storage_root / "metadata.db"

    @property
    def credential_store(self) -> BaseCredentialStore:
        """Return the shared credential-store adapter."""

        return self._session.active_credential_store

    def create_message_repository(self) -> SqliteMessageRepository:
        """Create a message repository for the calling thread."""

        return SqliteMessageRepository(self.connection_manager)

    def create_search_repository(self) -> BaseSearchRepository:
        """Create a read-only search repository for the calling thread."""

        return SqliteSearchRepository(self.connection_manager)

    def create_eml_storage(self) -> BaseEmlStorage:
        """Create an EML storage adapter bound to this root."""

        return EmlStorage(self.storage_root)

    def create_purge_storage(self) -> BasePurgeStorage:
        """Create the purge-capable storage adapter bound to this root."""

        return EmlStorage(self.storage_root)

    def create_manifest_writer(self, account_id: str) -> BaseManifestWriter:
        """Create an account-scoped manifest writer.

        The caller owns the returned writer and must close it after the sync
        operation.  Keeping it out of ``AppContext`` prevents cross-account
        and cross-thread file handles from being shared.
        """

        return ManifestWriter(self.storage_root, account_id)

    def create_manifest_reader(self, account_id: str) -> BaseManifestReader:
        """Create a read-only account-scoped manifest reader."""

        return ManifestReader(self.storage_root, account_id)

    def create_manifest_reader_all(self) -> BaseManifestReader:
        """Create a reader spanning all configured account manifests."""

        account_ids = set(self._manifest_account_ids())
        with suppress(Exception):
            account_ids.update(
                account_id
                for account in self.create_message_repository().list_accounts()
                if isinstance(account_id := account.get("id"), str) and account_id
            )
        return _CombinedManifestReader(
            tuple(self.create_manifest_reader(account_id) for account_id in sorted(account_ids))
        )

    def rebuild_database(
        self,
        *,
        cancel: Any = None,
        on_progress: Callable[[Any], None] | None = None,
    ) -> Any:
        """Rebuild and atomically replace the active metadata cache."""

        from mail_dock.infrastructure.database.reindex import rebuild_database

        return rebuild_database(
            self.database_path,
            self.create_eml_storage(),
            (
                self.create_manifest_reader(account_id)
                for account_id in self._manifest_account_ids()
            ),
            cancel=cancel,
            on_progress=on_progress,
        )

    def _manifest_account_ids(self) -> tuple[str, ...]:
        manifest_root = self.storage_root / "manifests" / "imap"
        try:
            return tuple(sorted(path.name for path in manifest_root.iterdir() if path.is_dir()))
        except OSError:
            return ()

    def create_fetcher(self, account: MessageRecord) -> BaseMailFetcher:
        """Create an authenticated fetcher for the calling worker thread."""

        account_id = account.get("id")
        host = account.get("host")
        username = account.get("username")
        port = account.get("port", 993)
        if not isinstance(account_id, str) or not account_id:
            raise ConfigError("Account must contain a valid id")
        if not isinstance(host, str) or not host:
            raise ConfigError(f"Account has no valid host: {account_id}")
        if not isinstance(username, str) or not username:
            raise ConfigError(f"Account has no valid username: {account_id}")
        if isinstance(port, bool) or not isinstance(port, int) or port <= 0:
            raise ConfigError(f"Account has no valid port: {account_id}")
        return OnamaeImapFetcher(
            host,
            username,
            load_credentials(self.credential_store, account_id),
            port=port,
            remote_trash_folder=self.settings.remote_trash_folder,
        )

    def create_fetcher_for_credentials(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
    ) -> BaseMailFetcher:
        """Create an unaffiliated fetcher for the setup connection test."""

        return OnamaeImapFetcher(
            host,
            username,
            password,
            port=port,
            remote_trash_folder=self.settings.remote_trash_folder,
        )

    def create_message_renderer(self) -> Any:
        """Create the renderer used by the open-message and save use cases."""

        if self._renderer_factory is not None:
            return self._renderer_factory()
        renderer_module = import_module("mail_dock.infrastructure.parsing.eml_render")
        return renderer_module.EmlMessageRenderer()

    def create_html_sanitizer(self) -> HtmlSanitizerFactory:
        """Create the Qt-independent sanitizer used by the mail preview."""

        sanitizer_module = import_module("mail_dock.infrastructure.parsing.html_sanitizer")
        return cast(HtmlSanitizerFactory, sanitizer_module.sanitize_mail_html)

    def stop_workers(self) -> None:
        """Stop presentation workers before the owning session is released."""

    def save_settings(self, settings: config.AppConfig) -> None:
        """Persist settings changes through the configuration module."""

        config.save(settings)
        self.settings = settings
        self._session.settings = settings

    def reprobe_storage(self) -> dict[str, object]:
        """Re-measure the active root and persist its current capability profile."""

        capabilities = probe_capabilities(self.storage_root)
        level = capability_level(capabilities)
        checked_path = os.path.normcase(str(self.storage_root.expanduser().resolve(strict=False)))
        fingerprint = storage_fingerprint(self.storage_root)
        if self.root_uuid is None:
            raise ConfigError("Active storage root has no UUID")
        profiles = dict(self.settings.storage_profiles)
        raw_profile = profiles.get(self.root_uuid)
        profile = dict(raw_profile) if isinstance(raw_profile, dict) else {}
        profile.update(
            {
                "capabilities": capabilities.as_dict(),
                "capability_level": level.value,
                "checked_path": checked_path,
                "storage_fingerprint": fingerprint,
            }
        )
        profiles[self.root_uuid] = profile
        updated = replace(self.settings, storage_profiles=profiles)
        config.save(updated)
        self.settings = updated
        self._session.settings = updated
        self.capabilities = capabilities.as_dict()
        self.capability_level = level.value
        self._session.capabilities = capabilities
        self._session.capability_level = level
        return {
            "capabilities": capabilities.as_dict(),
            "capability_level": level.value,
            "checked_path": checked_path,
            "storage_fingerprint": fingerprint,
        }

    def build_main_window(
        self,
        *,
        on_storage_root_switch: Callable[[Path], None] | None = None,
        on_storage_setup: Callable[[Path | None], None] | None = None,
        on_storage_detach: Callable[[], None] | None = None,
    ) -> Any:
        """Construct the main window through the presentation composition root."""

        from mail_dock.presentation.views.main_window import MainWindow

        window = MainWindow(
            self,
            on_storage_root_switch=(on_storage_root_switch or self.storage_root_switch_handler),
            on_storage_setup=on_storage_setup or self.storage_setup_handler,
            on_storage_detach=on_storage_detach or self.storage_detach_handler,
        )
        if self.window_created_handler is not None:
            self.window_created_handler(window)
        return window
