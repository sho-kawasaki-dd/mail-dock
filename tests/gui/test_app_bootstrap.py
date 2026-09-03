from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, cast

import pytest

from mail_dock import config
from mail_dock.domain.errors import StorageUnsupportedError
from mail_dock.domain.storage_state import StorageState, StorageStateMachine
from mail_dock.infrastructure.storage.capabilities import CapabilityLevel, StorageCapabilities
from mail_dock.infrastructure.storage.storage_root import RootProbe
from mail_dock.presentation import app
from mail_dock.usecases.trash import PurgeResult

pytestmark = pytest.mark.gui


class _FakeApplication:
    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.quit_calls = 0

    @staticmethod
    def instance() -> _FakeApplication:
        return _APPLICATION

    def exec(self) -> int:
        return self.exit_code

    def quit(self) -> None:
        self.quit_calls += 1


class _FakeSession:
    instances: ClassVar[list[_FakeSession]] = []
    fail_enter: ClassVar[bool] = False
    unsupported_remaining: ClassVar[int] = 0

    def __init__(self, _settings: config.AppConfig, root: Path) -> None:
        self.root = root
        self.root_uuid = "root-uuid"
        self.enter_calls = 0
        self.exit_calls = 0
        self.settings = _settings
        self.connection_manager = object()
        self.network_drive = False
        self.journal_mode = "WAL"
        self.__class__.instances.append(self)

    def __enter__(self) -> _FakeSession:
        self.enter_calls += 1
        if self.fail_enter:
            raise RuntimeError("session start failed")
        if self.unsupported_remaining > 0:
            self.__class__.unsupported_remaining -= 1
            raise StorageUnsupportedError(self.root_uuid, "unsupported")
        return self

    def __exit__(self, *_args: object) -> None:
        self.exit_calls += 1


class _FakeContext:
    instances: ClassVar[list[_FakeContext]] = []

    def __init__(self, session: _FakeSession, _settings: config.AppConfig) -> None:
        self.session = session
        self.save_calls = 0
        self.stop_calls = 0
        self.__class__.instances.append(self)

    def save_settings(self, _settings: config.AppConfig) -> None:
        self.save_calls += 1

    def stop_workers(self) -> None:
        self.stop_calls += 1


class _FakeWizard:
    callback: Any
    probe_callback: Any
    accepted: ClassVar[bool] = True
    events: ClassVar[list[str]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.callback = kwargs["on_root_confirmed"]
        self.probe_callback = kwargs["on_root_probe"]
        self.selected_root: Path | None = None
        self.__class__.events.append("wizard")

    def exec(self) -> int:
        if self.accepted:
            self.selected_root = Path("/attached/mail-dock")
            self.probe_callback(self.selected_root, "unknown")
            self.__class__.events.append("confirm")
            self.callback(self.selected_root)
            return app.QWizardAccepted
        self.__class__.events.append("cancel")
        return 0


class _FakeWindow:
    def __init__(self) -> None:
        self.stop_calls = 0

    def stop_workers(self) -> None:
        self.stop_calls += 1


class _PurgeManifest:
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class _PurgeContext:
    def __init__(self, mode: str) -> None:
        self.settings = config.AppConfig(purge_mode=mode)
        self.manifest_accounts: list[str] = []

    def create_message_repository(self) -> object:
        return object()

    def create_eml_storage(self) -> object:
        return object()

    def create_purge_storage(self) -> object:
        return object()

    def create_manifest_writer(self, account_id: str) -> _PurgeManifest:
        self.manifest_accounts.append(account_id)
        return _PurgeManifest(account_id)


class _FakeThread:
    def isRunning(self) -> bool:  # noqa: N802
        return False


_APPLICATION = _FakeApplication()


def test_rebase_root_candidates_follows_a_changed_drive_letter() -> None:
    rebased = app._rebase_root_candidates(
        (r"E:\mail-dock",),
        Path(r"E:\mail-dock"),
        ("F:",),
    )

    assert rebased == (Path(r"F:\mail-dock"),)


def test_device_arrival_uses_matching_uuid_on_a_changed_drive_letter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = config.AppConfig(
        storage_root_uuid="root-uuid",
        storage_root_candidates=(r"E:\mail-dock",),
    )
    runtime = app._GuiRuntime(cast(Any, object()), settings)
    runtime.session = cast(
        Any,
        type("Session", (), {"root": Path(r"E:\mail-dock"), "root_uuid": "root-uuid"})(),
    )
    arrived: list[bool] = []
    monitor = cast(
        Any,
        type(
            "Monitor",
            (),
            {
                "state": StorageState.DETACHED,
                "root": Path(r"E:\mail-dock"),
                "handle_device_arrived": lambda self: arrived.append(True),
            },
        )(),
    )
    runtime.storage_monitor = monitor
    resolution_calls: list[tuple[tuple[Path, ...], str | None]] = []

    def resolve(candidates: tuple[Path, ...], expected_uuid: str | None) -> Any:
        resolution_calls.append((candidates, expected_uuid))
        return type("Resolution", (), {"path": Path(r"F:\mail-dock"), "probe": RootProbe.OK})()

    monkeypatch.setattr(app, "resolve_root", resolve)

    runtime._handle_device_arrived(("F:",))

    assert monitor.root == Path(r"F:\mail-dock")
    assert arrived == [True]
    assert resolution_calls == [((Path(r"F:\mail-dock"),), "root-uuid")]


def test_device_arrival_rejects_a_foreign_uuid_root(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = config.AppConfig(
        storage_root_uuid="root-uuid",
        storage_root_candidates=(r"E:\mail-dock",),
    )
    runtime = app._GuiRuntime(cast(Any, object()), settings)
    runtime.session = cast(
        Any,
        type("Session", (), {"root": Path(r"E:\mail-dock"), "root_uuid": "root-uuid"})(),
    )
    arrived: list[bool] = []
    monitor = cast(
        Any,
        type(
            "Monitor",
            (),
            {
                "state": StorageState.DETACHED,
                "root": Path(r"E:\mail-dock"),
                "handle_device_arrived": lambda self: arrived.append(True),
            },
        )(),
    )
    runtime.storage_monitor = monitor

    monkeypatch.setattr(
        app,
        "resolve_root",
        lambda _candidates, _expected: type(
            "Resolution", (), {"path": Path(r"F:\mail-dock"), "probe": RootProbe.FOREIGN}
        )(),
    )

    runtime._handle_device_arrived(("F:",))

    assert monitor.root == Path(r"E:\mail-dock")
    assert arrived == []


def _start_fake_session(
    settings: config.AppConfig,
    root: Path,
) -> tuple[_FakeSession, _FakeContext]:
    session = _FakeSession(settings, root)
    session.__enter__()
    return session, _FakeContext(session, settings)


def test_run_gui_starts_session_only_after_root_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeSession.instances.clear()
    _FakeContext.instances.clear()
    _FakeWizard.events.clear()
    window = _FakeWindow()
    errors: list[BaseException] = []

    monkeypatch.setattr(app, "QApplication", _FakeApplication)
    monkeypatch.setattr(app, "register_schemes", lambda: None)
    monkeypatch.setattr(app, "_show_error", errors.append)
    monkeypatch.setattr(app, "_available_root", lambda _settings, _requested: None)
    monkeypatch.setattr(app, "StorageSession", _FakeSession)
    monkeypatch.setattr(app, "AppContext", _FakeContext)
    monkeypatch.setattr(app, "SetupWizard", _FakeWizard)
    monkeypatch.setattr(
        app,
        "_probe_setup_root",
        lambda settings, _root, _encryption: (settings, {}),
    )
    monkeypatch.setattr(
        app,
        "_commit_setup_root",
        lambda settings, _root, _result: settings,
    )

    monkeypatch.setattr(app, "_start_session", _start_fake_session)
    monkeypatch.setattr(
        app,
        "_start_verification",
        lambda _application, _session, _context: (_FakeThread(), {"error": None, "window": window}),
    )

    result = app.run_gui(config.AppConfig())
    assert result == 0, errors
    assert _FakeWizard.events == ["wizard", "confirm"]
    assert len(_FakeSession.instances) == 1
    assert _FakeSession.instances[0].enter_calls == 1
    assert _FakeSession.instances[0].exit_calls == 1
    assert _FakeContext.instances[0].save_calls == 1
    assert window.stop_calls == 1
    assert errors == []


def test_immediate_startup_purge_does_not_prompt_and_separates_manifests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _PurgeContext("immediate")
    candidates = (
        {"id": 1, "account_id": "account-a", "subject": "first", "size_bytes": 10},
        {"id": 2, "account_id": "account-b", "subject": "second", "size_bytes": 20},
    )
    purge_calls: list[tuple[str, tuple[int, ...]]] = []

    monkeypatch.setattr(app, "list_startup_purge_candidates", lambda *_args, **_kwargs: candidates)
    monkeypatch.setattr(
        app,
        "ConfirmationDialog",
        lambda *_args, **_kwargs: pytest.fail("immediate purge must not prompt"),
    )

    def fake_purge(
        _repo: object,
        _storage: object,
        manifest: _PurgeManifest,
        **kwargs: object,
    ) -> PurgeResult:
        message_ids = kwargs["message_ids"]
        assert isinstance(message_ids, list)
        purge_calls.append((manifest.account_id, tuple(message_ids)))
        return PurgeResult(purged_ids=tuple(message_ids))

    monkeypatch.setattr(app, "purge", fake_purge)

    result = app._run_startup_purge(cast(Any, context))

    assert result is not None
    assert result.purged_ids == (1, 2)
    assert purge_calls == [("account-a", (1,)), ("account-b", (2,))]
    assert context.manifest_accounts == ["account-a", "account-b"]


def test_grace_startup_purge_shows_candidates_and_honors_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _PurgeContext("grace")
    candidates = ({"id": 1, "account_id": "account-a", "subject": "old mail", "size_bytes": 10},)
    messages: list[str] = []
    purge_calls: list[object] = []

    monkeypatch.setattr(app, "list_startup_purge_candidates", lambda *_args, **_kwargs: candidates)

    class RejectDialog:
        def __init__(self, message: str, _parent: object) -> None:
            messages.append(message)

        def confirmed(self) -> bool:
            return False

    monkeypatch.setattr(app, "ConfirmationDialog", RejectDialog)
    monkeypatch.setattr(app, "purge", lambda *_args, **_kwargs: purge_calls.append(True))

    result = app._run_startup_purge(cast(Any, context))

    assert result is not None
    assert result.skipped_ids == (1,)
    assert purge_calls == []
    assert "old mail" in messages[0]
    assert "1 件" in messages[0]


def test_setup_root_probe_does_not_initialize_or_persist_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = config.AppConfig()
    saved: list[config.AppConfig] = []
    capabilities = StorageCapabilities(
        exclusive_lock=True,
        replace_overwrite=True,
        wal_supported=False,
        fsync_supported=True,
        case_sensitive=True,
        long_path_ok=True,
        checked_at="2026-08-05T00:00:00+00:00",
    )
    monkeypatch.setattr(app, "probe_capabilities", lambda _root: capabilities)
    monkeypatch.setattr(app, "capability_level", lambda _capabilities: CapabilityLevel.DEGRADED)
    monkeypatch.setattr(app, "storage_fingerprint", lambda _root: "device:1")
    monkeypatch.setattr(app.config, "save", saved.append)
    monkeypatch.setattr(app.config, "load", lambda: saved[-1])

    loaded, result = app._probe_setup_root(settings, tmp_path / "root", "unknown")

    assert result["capability_level"] == "degraded"
    assert result["encryption"] == "unknown"
    assert loaded == settings
    assert saved == []


def test_run_gui_cancelled_wizard_does_not_create_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeSession.instances.clear()
    _FakeWizard.events.clear()
    _FakeWizard.accepted = False

    monkeypatch.setattr(app, "QApplication", _FakeApplication)
    monkeypatch.setattr(app, "register_schemes", lambda: None)
    monkeypatch.setattr(app, "_available_root", lambda _settings, _requested: None)
    monkeypatch.setattr(app, "StorageSession", _FakeSession)
    monkeypatch.setattr(app, "SetupWizard", _FakeWizard)

    try:
        assert app.run_gui(config.AppConfig()) == 0
    finally:
        _FakeWizard.accepted = True

    assert _FakeWizard.events == ["wizard", "cancel"]
    assert _FakeSession.instances == []


def test_run_gui_releases_a_partially_started_session_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeSession.instances.clear()
    _FakeSession.fail_enter = True
    _FakeWizard.events.clear()
    errors: list[BaseException] = []

    monkeypatch.setattr(app, "QApplication", _FakeApplication)
    monkeypatch.setattr(app, "register_schemes", lambda: None)
    monkeypatch.setattr(app, "_available_root", lambda _settings, _requested: None)
    monkeypatch.setattr(app, "StorageSession", _FakeSession)
    monkeypatch.setattr(app, "SetupWizard", _FakeWizard)
    monkeypatch.setattr(
        app,
        "_probe_setup_root",
        lambda settings, _root, _encryption: (settings, {}),
    )
    monkeypatch.setattr(
        app,
        "_commit_setup_root",
        lambda settings, _root, _result: settings,
    )

    def start_partial_session(
        settings: config.AppConfig,
        root: Path,
    ) -> tuple[_FakeSession, _FakeContext]:
        session = _FakeSession(settings, root)
        try:
            session.__enter__()
        except BaseException as error:
            session.__exit__(type(error), error, error.__traceback__)
            raise
        return session, _FakeContext(session, settings)

    monkeypatch.setattr(app, "_start_session", start_partial_session)
    monkeypatch.setattr(app, "_show_error", errors.append)

    try:
        result = app.run_gui(config.AppConfig())
        assert result == 1, errors
    finally:
        _FakeSession.fail_enter = False

    assert len(_FakeSession.instances) == 1
    assert _FakeSession.instances[0].enter_calls == 1
    assert _FakeSession.instances[0].exit_calls == 1
    assert len(errors) == 1


def test_run_gui_acknowledges_unsupported_existing_root_once_and_recreates_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeSession.instances.clear()
    _FakeSession.unsupported_remaining = 1
    reloaded_settings = config.AppConfig(sync_on_startup=False)
    acknowledged: list[StorageUnsupportedError] = []
    window = _FakeWindow()

    monkeypatch.setattr(app, "QApplication", _FakeApplication)
    monkeypatch.setattr(app, "register_schemes", lambda: None)
    monkeypatch.setattr(app, "_available_root", lambda _settings, _requested: Path("/attached"))
    monkeypatch.setattr(app, "StorageSession", _FakeSession)
    monkeypatch.setattr(app, "AppContext", _FakeContext)
    monkeypatch.setattr(app, "_start_session", _start_fake_session)

    def acknowledge(error: StorageUnsupportedError) -> bool:
        acknowledged.append(error)
        return True

    monkeypatch.setattr(
        app,
        "_confirm_storage_unsupported",
        acknowledge,
    )
    monkeypatch.setattr(
        app,
        "_acknowledge_storage_unsupported",
        lambda _settings, error: reloaded_settings,
    )
    monkeypatch.setattr(
        app,
        "_start_verification",
        lambda _application, _session, _context: (_FakeThread(), {"error": None, "window": window}),
    )

    assert app.run_gui(config.AppConfig()) == 0

    assert len(acknowledged) == 1
    assert len(_FakeSession.instances) == 2
    assert _FakeSession.instances[1].settings is reloaded_settings
    assert _FakeSession.instances[1].exit_calls == 1


def test_run_gui_rejects_unsupported_existing_root_with_exit_code_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeSession.instances.clear()
    _FakeSession.unsupported_remaining = 1

    monkeypatch.setattr(app, "QApplication", _FakeApplication)
    monkeypatch.setattr(app, "register_schemes", lambda: None)
    monkeypatch.setattr(app, "_available_root", lambda _settings, _requested: Path("/attached"))
    monkeypatch.setattr(app, "StorageSession", _FakeSession)
    monkeypatch.setattr(app, "_confirm_storage_unsupported", lambda _error: False)
    monkeypatch.setattr(
        app,
        "_start_verification",
        lambda *_args: pytest.fail("verification must not start after rejection"),
    )

    try:
        assert app.run_gui(config.AppConfig()) == 3
    finally:
        _FakeSession.unsupported_remaining = 0

    assert len(_FakeSession.instances) == 1


def test_acknowledge_storage_unsupported_persists_timestamp_and_reloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = config.AppConfig(
        storage_profiles={
            "root-uuid": {
                "capability_level": "unsupported",
                "capabilities": {},
                "checked_path": "/attached",
                "storage_fingerprint": "posix:1:/attached",
            }
        }
    )
    error = StorageUnsupportedError("root-uuid", "unsupported")
    saved: list[config.AppConfig] = []
    reloaded = config.AppConfig(sync_on_startup=False)
    monkeypatch.setattr(app.config, "save", saved.append)
    monkeypatch.setattr(app.config, "load", lambda: reloaded)

    assert app._acknowledge_storage_unsupported(settings, error) is reloaded

    assert len(saved) == 1
    acknowledged = saved[0].storage_profiles["root-uuid"]
    assert isinstance(acknowledged, dict)
    assert isinstance(acknowledged["capability_ack_at"], str)


class _RecoverySession:
    def __init__(self, unclean: bool) -> None:
        self.settings = config.AppConfig()
        self.root = Path("/attached")
        self.was_unclean_shutdown = unclean

    def __enter__(self) -> _RecoverySession:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _RecoveryContext:
    def __init__(self, session: _RecoverySession, _settings: config.AppConfig) -> None:
        self.storage_root = session.root

    def create_message_repository(self) -> str:
        return "repo"

    def create_eml_storage(self) -> str:
        return "eml-storage"

    def create_purge_storage(self) -> str:
        return "purge-storage"

    def create_manifest_reader(self, account_id: str) -> str:
        return f"reader:{account_id}"

    def create_manifest_writer(self, account_id: str) -> str:
        return f"writer:{account_id}"


def test_start_session_runs_range_verify_and_purge_recovery_after_unclean_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    monkeypatch.setattr(app, "StorageSession", lambda _settings, _root: _RecoverySession(True))
    monkeypatch.setattr(app, "AppContext", _RecoveryContext)
    monkeypatch.setattr(
        app, "backfill_snapshots", lambda *_args, **_kwargs: calls.append("backfill")
    )
    monkeypatch.setattr(
        app, "repair_manifest_tails", lambda *_args, **_kwargs: calls.append("repair")
    )

    def fake_recover(
        repo: object,
        storage: object,
        purge_storage: object,
        reader_factory: Any,
        writer_factory: Any,
        *,
        storage_state: object,
    ) -> tuple[object, ...]:
        calls.append("recover")
        assert repo == "repo"
        assert storage == "eml-storage"
        assert purge_storage == "purge-storage"
        assert reader_factory("account") == "reader:account"
        assert writer_factory("account") == "writer:account"
        assert isinstance(storage_state, StorageStateMachine)
        return ()

    monkeypatch.setattr(app, "recover_after_unclean_shutdown", fake_recover)

    app._start_session(config.AppConfig(), Path("/attached"))

    assert calls == ["backfill", "repair", "recover"]


def test_start_session_skips_recovery_after_a_clean_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    monkeypatch.setattr(app, "StorageSession", lambda _settings, _root: _RecoverySession(False))
    monkeypatch.setattr(app, "AppContext", _RecoveryContext)
    monkeypatch.setattr(
        app, "backfill_snapshots", lambda *_args, **_kwargs: calls.append("backfill")
    )
    monkeypatch.setattr(
        app,
        "repair_manifest_tails",
        lambda *_args, **_kwargs: pytest.fail("must not run without an unclean shutdown"),
    )
    monkeypatch.setattr(
        app,
        "recover_after_unclean_shutdown",
        lambda *_args, **_kwargs: pytest.fail("must not run without an unclean shutdown"),
    )

    app._start_session(config.AppConfig(), Path("/attached"))

    assert calls == ["backfill"]
