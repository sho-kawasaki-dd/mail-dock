"""GUI bootstrap and lifetime management."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from PySide6.QtCore import QCoreApplication, QObject, Qt, QThread, Signal, Slot
from PySide6.QtWidgets import QApplication, QDialog

from mail_dock import config as config
from mail_dock.__main__ import (
    StorageSession,
    _exit_code,
    _verify_database,
    _verify_fts_database,
)
from mail_dock.domain.errors import (
    ConfigError,
    MailDockError,
    StorageForeignRootError,
    StorageUnsupportedError,
)
from mail_dock.infrastructure.storage.capabilities import (
    capability_level,
    probe_capabilities,
    storage_fingerprint,
)
from mail_dock.infrastructure.storage.eml_storage import cleanup_tmp
from mail_dock.infrastructure.storage.storage_root import (
    RootProbe,
    check_free_space,
    drive_kind,
    ensure_layout,
    free_space,
    initialize_root,
    probe,
    resolve_root,
)
from mail_dock.presentation import strings
from mail_dock.presentation.context import AppContext
from mail_dock.presentation.views.dialogs.confirmation_dialog import ConfirmationDialog
from mail_dock.presentation.views.dialogs.error_dialog import show_error
from mail_dock.presentation.views.setup_wizard import SetupWizard
from mail_dock.presentation.web.schemes import register_schemes

LOGGER = logging.getLogger(__name__)


class _StartupVerificationWorker(QObject):
    """Run startup database checks away from the Qt GUI thread."""

    finished = Signal()
    failed = Signal(object)

    def __init__(self, session: StorageSession, mode: str) -> None:
        super().__init__()
        self._session = session
        self._mode = mode

    @Slot()
    def run(self) -> None:
        try:
            connection = self._session.connection_manager.get_connection()
            _verify_database(connection)
            self._session.connection_manager.close_current_thread()
            if self._mode == "full":
                _verify_fts_database(
                    self._session.root / "metadata.db",
                    journal_mode=self._session.journal_mode or "DELETE",
                )
        except BaseException as error:
            self.failed.emit(error)
        else:
            self.finished.emit()


class _StartupVerificationCompletion(QObject):
    """Handle verification results on the GUI thread."""

    def __init__(
        self,
        app: QCoreApplication,
        session: StorageSession,
        context: AppContext,
        result: dict[str, Any],
        window_factory: Callable[[AppContext], Any] | None = None,
    ) -> None:
        super().__init__(app)
        self._app = app
        self._session = session
        self._context = context
        self._result = result
        self._window_factory = window_factory

    @Slot()
    def verified(self) -> None:
        if self._window_factory is not None:
            window = self._window_factory(self._context)
        else:
            window = self._context.build_main_window()
        self._result["window"] = window
        window.show()
        if self._session.settings.sync_on_startup:
            start_sync = getattr(window, "start_startup_sync", None)
            if callable(start_sync):
                start_sync()

    @Slot(object)
    def failed(self, error: object) -> None:
        if isinstance(error, BaseException):
            self._result["error"] = error
            _show_error(error)
        self._app.quit()


def _available_root(settings: config.AppConfig, requested_root: Path | None) -> Path | None:
    """Resolve a known root without creating a marker or acquiring a lock."""

    candidates = (
        (requested_root,)
        if requested_root is not None
        else tuple(Path(candidate) for candidate in settings.storage_root_candidates)
    )
    resolution = resolve_root(candidates, settings.storage_root_uuid)
    if resolution.probe is RootProbe.FOREIGN:
        raise StorageForeignRootError(strings.ERROR_FOREIGN_ROOT)
    return resolution.path


def _show_error(error: BaseException) -> None:
    show_error(error)


def _confirm_storage_unsupported(error: StorageUnsupportedError) -> bool:
    del error
    return ConfirmationDialog(strings.DIALOG_CONFIRM_STORAGE_UNSUPPORTED).confirmed()


def _acknowledge_storage_unsupported(
    settings: config.AppConfig,
    error: StorageUnsupportedError,
) -> config.AppConfig:
    profiles = dict(settings.storage_profiles)
    raw_profile = profiles.get(error.root_uuid)
    if not isinstance(raw_profile, dict):
        raise ConfigError("Storage capability result is missing from configuration")
    profile = dict(raw_profile)
    profile["capability_ack_at"] = datetime.now(UTC).isoformat()
    profiles[error.root_uuid] = profile
    acknowledged = replace(settings, storage_profiles=profiles)
    config.save(acknowledged)
    return config.load()


def _probe_setup_root(
    settings: config.AppConfig,
    root: Path,
    encryption: str,
) -> tuple[config.AppConfig, dict[str, object]]:
    """Probe a newly selected root without initializing its archive layout."""

    checked_path = _normalized_storage_path(root)
    fingerprint = storage_fingerprint(root)
    capabilities = probe_capabilities(root)
    level = capability_level(capabilities)
    return settings, {
        "capabilities": capabilities.as_dict(),
        "capability_level": level.value,
        "checked_path": checked_path,
        "storage_fingerprint": fingerprint,
        "encryption": encryption,
    }


def _commit_setup_root(
    settings: config.AppConfig,
    root: Path,
    result: Mapping[str, object],
) -> config.AppConfig:
    """Initialize a confirmed root and persist its completed probe result."""

    marker = initialize_root(root)
    ensure_layout(root)
    cleanup_tmp(root)
    capabilities = result.get("capabilities")
    capability_level_value = result.get("capability_level")
    checked_path = result.get("checked_path")
    fingerprint = result.get("storage_fingerprint")
    encryption = result.get("encryption")
    if not isinstance(capabilities, dict):
        raise ConfigError("Storage capability result is invalid")
    if not isinstance(capability_level_value, str):
        raise ConfigError("Storage capability result is invalid")
    if not isinstance(checked_path, str):
        raise ConfigError("Storage capability result is invalid")
    if not isinstance(fingerprint, str):
        raise ConfigError("Storage capability result is invalid")
    if not isinstance(encryption, str):
        raise ConfigError("Storage capability result is invalid")
    capabilities_value = cast(config.JSONValue, capabilities)
    profile_updates: dict[str, config.JSONValue] = {
        "capabilities": capabilities_value,
        "capability_level": capability_level_value,
        "checked_path": checked_path,
        "storage_fingerprint": fingerprint,
        "encryption": encryption,
        "encryption_declared_at": datetime.now(UTC).isoformat(),
    }
    profiles = dict(settings.storage_profiles)
    raw_profile = profiles.get(marker.root_uuid)
    profile = dict(raw_profile) if isinstance(raw_profile, dict) else {}
    profile.update(profile_updates)
    updated = replace(
        settings,
        storage_root_uuid=marker.root_uuid,
        storage_root_candidates=(_normalized_storage_path(root),),
        storage_profiles={**profiles, marker.root_uuid: profile},
    )
    config.save(updated)
    return config.load()


def _normalized_storage_path(root: Path) -> str:
    return os.path.normcase(str(root.expanduser().resolve(strict=False)))


def _stop_window(window: Any, context: AppContext | None) -> None:
    stop_workers = getattr(window, "stop_workers", None)
    if callable(stop_workers):
        stop_workers()
    elif context is not None:
        context.stop_workers()


def _start_verification(
    app: QCoreApplication,
    session: StorageSession,
    context: AppContext,
    *,
    window_factory: Callable[[AppContext], Any] | None = None,
) -> tuple[QThread, dict[str, Any]]:
    thread = QThread()
    worker = _StartupVerificationWorker(session, session.settings.startup_verification)
    worker.moveToThread(thread)
    result: dict[str, Any] = {"error": None, "window": None, "worker": worker}
    completion = _StartupVerificationCompletion(
        app,
        session,
        context,
        result,
        window_factory,
    )
    result["completion"] = completion

    thread.started.connect(worker.run)
    worker.finished.connect(completion.verified, Qt.ConnectionType.QueuedConnection)
    worker.failed.connect(completion.failed, Qt.ConnectionType.QueuedConnection)
    worker.finished.connect(thread.quit)
    worker.failed.connect(thread.quit)
    worker.finished.connect(worker.deleteLater)
    worker.failed.connect(worker.deleteLater)
    thread.start()
    return thread, result


QWizardAccepted = 1


def _start_session(
    settings: config.AppConfig,
    root: Path,
) -> tuple[StorageSession, AppContext]:
    """Start one storage session and compose its presentation context."""

    session = StorageSession(settings, root)
    try:
        session.__enter__()
        return session, AppContext(session, session.settings)
    except BaseException as error:
        session.__exit__(type(error), error, error.__traceback__)
        raise


class _GuiRuntime:
    """Own the replaceable GUI window and exactly one active storage session."""

    def __init__(self, application: QCoreApplication, settings: config.AppConfig) -> None:
        self.application = application
        self.settings = settings
        self.session: StorageSession | None = None
        self.context: AppContext | None = None
        self.window: Any = None
        self.verification_thread: QThread | None = None
        self.verification_result: dict[str, Any] = {"error": None, "window": None}

    def attach(self, session: StorageSession, context: AppContext) -> None:
        self.session = session
        self.context = context
        self.settings = session.settings
        context.storage_root_switch_handler = self.switch_root
        context.storage_setup_handler = self.start_setup
        context.window_created_handler = self._window_created

    def start(self, root: Path) -> AppContext:
        session, context = _start_session(self.settings, root)
        self.attach(session, context)
        return context

    def _window_created(self, window: Any) -> None:
        self.window = window

    def verify_and_show(self) -> None:
        if self.session is None or self.context is None:
            raise RuntimeError("GUI session has not started")
        self.verification_thread, self.verification_result = _start_verification(
            self.application,
            self.session,
            self.context,
            window_factory=lambda context: context.build_main_window(),
        )

    def _release_current(self) -> None:
        if self.verification_thread is not None and self.verification_thread.isRunning():
            self.verification_thread.quit()
            self.verification_thread.wait()
        if self.window is not None:
            _stop_window(self.window, self.context)
            # Prevent Qt's quitOnLastWindowClosed from ending app.exec() before
            # verify_and_show() shows the replacement window (root switch/setup).
            set_attribute = getattr(self.window, "setAttribute", None)
            if callable(set_attribute):
                set_attribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
            close = getattr(self.window, "close", None)
            if callable(close):
                close()
        elif self.context is not None:
            self.context.stop_workers()
        if self.session is not None:
            self.session.__exit__(None, None, None)
        self.window = None
        self.context = None
        self.session = None
        self.verification_thread = None

    def switch_root(self, root: Path) -> None:
        result = probe(root, None)
        if result is RootProbe.MISSING:
            if ConfirmationDialog(strings.DIALOG_CONFIRM_SETUP_MISSING_ROOT).confirmed():
                self.start_setup(root)
            return
        if result is RootProbe.FOREIGN:
            _show_error(StorageForeignRootError(strings.ERROR_FOREIGN_ROOT))
            return
        self._replace_with_root(root)

    def _replace_with_root(self, root: Path) -> None:
        marker = initialize_root(root)
        self._release_current()
        self.settings = replace(
            self.settings,
            storage_root_uuid=marker.root_uuid,
            storage_root_candidates=(_normalized_storage_path(root),),
        )
        context = self.start(root)
        updated = replace(
            context.settings,
            storage_root_uuid=context.root_uuid,
            storage_root_candidates=(_normalized_storage_path(root),),
        )
        context.save_settings(updated)
        self.verify_and_show()

    def start_setup(self, initial_root: Path | None = None) -> None:
        old_root = self.context.storage_root if self.context is not None else None
        old_settings = self.settings
        pending_probe: dict[str, object] | None = None

        def probe_root(selected_root: Path, encryption: str) -> Mapping[str, object]:
            nonlocal pending_probe
            _unchanged_settings, result = _probe_setup_root(
                self.settings,
                selected_root,
                encryption,
            )
            pending_probe = result
            return result

        def start_session(selected_root: Path) -> AppContext:
            if pending_probe is None:
                raise ConfigError("Storage root must be probed before confirmation")
            self._release_current()
            self.settings = _commit_setup_root(self.settings, selected_root, pending_probe)
            return self.start(selected_root)

        wizard = SetupWizard(
            initial_root=initial_root,
            context=None,
            expected_root_uuid=self.settings.storage_root_uuid,
            on_root_confirmed=start_session,
            on_root_probe=probe_root,
            on_root_identity_probe=lambda path: probe(path, self.settings.storage_root_uuid).value,
            check_root_space=lambda path: check_free_space(path).value,
            resolve_drive_kind=lambda path: drive_kind(path).value,
            resolve_free_space=free_space,
        )
        accepted = wizard.exec() == QDialog.DialogCode.Accepted
        if accepted and self.session is not None:
            self.verify_and_show()
        elif not accepted and old_root is not None and self.session is not None:
            current_root = self.context.storage_root if self.context is not None else None
            if current_root != old_root:
                self._release_current()
                self.settings = old_settings
                config.save(old_settings)
                self.start(old_root)
                self.verify_and_show()

    def close(self) -> None:
        self._release_current()


def run_gui(settings: config.AppConfig, *, requested_root: Path | None = None) -> int:
    """Run the GUI and return its process exit code.

    Scheme registration intentionally precedes QApplication construction. A
    storage session is created only after root selection has completed, which
    keeps first-run bootstrap usable without a configured or attached drive.
    """

    register_schemes()
    app = QApplication.instance() or QApplication(sys.argv)
    session: StorageSession | None = None
    context: AppContext | None = None
    window: Any = None
    verification_thread: QThread | None = None
    verification_result: dict[str, Any] = {"error": None, "window": None}
    session_error: BaseException | None = None
    runtime: _GuiRuntime | None = None
    try:
        active_settings = settings
        root = _available_root(active_settings, requested_root)
        setup_wizard: SetupWizard | None = None
        if root is None:
            pending_probe: dict[str, object] | None = None

            def probe_root(selected_root: Path, encryption: str) -> Mapping[str, object]:
                nonlocal pending_probe
                _unchanged_settings, result = _probe_setup_root(
                    active_settings,
                    selected_root,
                    encryption,
                )
                pending_probe = result
                return result

            def start_session(selected_root: Path) -> AppContext:
                nonlocal active_settings, session, context
                if pending_probe is None:
                    raise ConfigError("Storage root must be probed before confirmation")
                active_settings = _commit_setup_root(
                    active_settings,
                    selected_root,
                    pending_probe,
                )
                session, context = _start_session(active_settings, selected_root)
                updated_settings = replace(
                    session.settings,
                    storage_root_uuid=session.root_uuid,
                    storage_root_candidates=(_normalized_storage_path(selected_root),),
                )
                context.save_settings(updated_settings)
                return context

            setup_wizard = SetupWizard(
                initial_root=requested_root,
                expected_root_uuid=active_settings.storage_root_uuid,
                on_root_confirmed=start_session,
                on_root_probe=probe_root,
                on_root_identity_probe=lambda path: probe(
                    path,
                    active_settings.storage_root_uuid,
                ).value,
                check_root_space=lambda path: check_free_space(path).value,
                resolve_drive_kind=lambda path: drive_kind(path).value,
                resolve_free_space=free_space,
            )
            if setup_wizard.exec() != QDialog.DialogCode.Accepted:
                return 0
            root = setup_wizard.selected_root
            if root is None or session is None or context is None:
                return 1
        else:
            unsupported_acknowledged = False
            while True:
                try:
                    session, context = _start_session(active_settings, root)
                except StorageUnsupportedError as unsupported_error:
                    if unsupported_acknowledged or not _confirm_storage_unsupported(
                        unsupported_error
                    ):
                        session_error = unsupported_error
                        return _exit_code(unsupported_error)
                    active_settings = _acknowledge_storage_unsupported(
                        active_settings, unsupported_error
                    )
                    unsupported_acknowledged = True
                    continue
                break
        runtime = _GuiRuntime(app, active_settings)
        runtime.attach(session, context)
        verification_thread, verification_result = _start_verification(app, session, context)
        runtime.verification_thread = verification_thread
        runtime.verification_result = verification_result
        event_code = app.exec()
        window = verification_result["window"]
        runtime.window = window
        error = verification_result["error"]
        if isinstance(error, MailDockError):
            session_error = error
            return _exit_code(error)
        if error is not None:
            session_error = error
            return 1
        return event_code
    except BaseException as error:
        session_error = error
        _show_error(error)
        return _exit_code(error) if isinstance(error, MailDockError) else 1
    finally:
        if runtime is not None:
            runtime.close()
            session = None
            context = None
            window = None
            verification_thread = None
        if verification_thread is not None and verification_thread.isRunning():
            verification_thread.quit()
            verification_thread.wait()
        if window is not None:
            _stop_window(window, context)
        elif context is not None:
            context.stop_workers()
        if session is not None:
            if session_error is None:
                session.__exit__(None, None, None)
            else:
                session.__exit__(type(session_error), session_error, session_error.__traceback__)
