from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mail_dock.infrastructure.storage.storage_root import DriveKind, SpaceStatus
from mail_dock.presentation import strings
from mail_dock.presentation.views.setup_wizard import SetupWizard

pytestmark = pytest.mark.gui


class _Repository:
    def __init__(self) -> None:
        self.targets: list[tuple[str, str, bool]] = []

    def set_sync_target(self, account_id: str, raw_name: str, enabled: bool) -> None:
        self.targets.append((account_id, raw_name, enabled))


class _Context:
    def __init__(self) -> None:
        self.repository = _Repository()

    def create_message_repository(self) -> _Repository:
        return self.repository


def test_wizard_has_three_pages_and_confirms_a_temporary_root(
    tmp_path: Path,
    qtbot: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "mail_dock.presentation.views.setup_wizard.check_free_space",
        lambda _root: SpaceStatus.OK,
    )
    monkeypatch.setattr(
        "mail_dock.presentation.views.setup_wizard.free_space",
        lambda _root: 1024 * 1024 * 1024,
    )
    monkeypatch.setattr(
        "mail_dock.presentation.views.setup_wizard.drive_kind",
        lambda _root: DriveKind.LOCAL,
    )
    context = _Context()
    confirmed: list[Path] = []

    def record_confirmation(root: Path) -> _Context:
        confirmed.append(root)
        return context

    wizard = SetupWizard(
        initial_root=tmp_path / "mail-root",
        on_root_confirmed=record_confirmation,
    )
    qtbot.addWidget(wizard)
    wizard.show()

    assert len(wizard.pageIds()) == 3
    assert wizard.validateCurrentPage()
    assert wizard.selected_root == (tmp_path / "mail-root").resolve()
    assert confirmed == [wizard.selected_root]


def test_account_validation_rejects_invalid_id_before_connection_test(qtbot: Any) -> None:
    wizard = SetupWizard(context=_Context())
    qtbot.addWidget(wizard)
    wizard.show()
    wizard._account_id_edit.setText("bad id")
    wizard._host_edit.setText("imap.example.com")
    wizard._username_edit.setText("user")
    wizard._password_edit.setText("password")

    assert not wizard._validate_account()
    assert wizard._account_status.text()


def test_folder_validation_requires_selection_and_persists_selected_targets(qtbot: Any) -> None:
    context = _Context()
    wizard = SetupWizard(context=context)
    qtbot.addWidget(wizard)
    wizard.show()
    wizard._account_id = "account-1"
    wizard._set_folder_checks(
        (
            {"raw_name": "INBOX", "display_name": "受信箱", "is_sync_target": 0},
            {"raw_name": "Archive", "display_name": "アーカイブ", "is_sync_target": 0},
        )
    )

    assert not wizard._validate_folders()
    wizard._folder_checks[1].setChecked(True)
    assert wizard._validate_folders()
    assert context.repository.targets == [
        ("account-1", "INBOX", False),
        ("account-1", "Archive", True),
    ]


def test_unsupported_capability_blocks_root_confirmation(
    tmp_path: Path,
    qtbot: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "mail_dock.presentation.views.setup_wizard.check_free_space",
        lambda _root: SpaceStatus.OK,
    )
    monkeypatch.setattr(
        "mail_dock.presentation.views.setup_wizard.free_space",
        lambda _root: 1024 * 1024 * 1024,
    )
    monkeypatch.setattr(
        "mail_dock.presentation.views.setup_wizard.drive_kind",
        lambda _root: DriveKind.LOCAL,
    )
    confirmed: list[Path] = []
    wizard = SetupWizard(
        initial_root=tmp_path / "mail-root",
        on_root_confirmed=lambda root: confirmed.append(root),
        on_root_probe=lambda _root, _encryption: {"capability_level": "unsupported"},
    )
    qtbot.addWidget(wizard)
    wizard.show()

    assert not wizard.validateCurrentPage()
    assert wizard._capability_label.text() == strings.WIZARD_CAPABILITY_UNSUPPORTED
    assert confirmed == []


def test_degraded_capability_and_unencrypted_declaration_require_confirmation(
    tmp_path: Path,
    qtbot: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "mail_dock.presentation.views.setup_wizard.check_free_space",
        lambda _root: SpaceStatus.OK,
    )
    monkeypatch.setattr(
        "mail_dock.presentation.views.setup_wizard.free_space",
        lambda _root: 1024 * 1024 * 1024,
    )
    monkeypatch.setattr(
        "mail_dock.presentation.views.setup_wizard.drive_kind",
        lambda _root: DriveKind.LOCAL,
    )
    declarations: list[str] = []
    context = _Context()
    wizard = SetupWizard(
        initial_root=tmp_path / "mail-root",
        on_root_probe=lambda _root, encryption: (
            declarations.append(encryption) or {"capability_level": "degraded"}
        ),
        on_root_confirmed=lambda _root: context,
    )
    qtbot.addWidget(wizard)
    wizard.show()
    wizard._encryption_combo.setCurrentIndex(1)

    assert not wizard.validateCurrentPage()
    assert declarations == []
    assert wizard._encryption_confirmation.isVisible()

    wizard._encryption_confirmation.setChecked(True)
    assert wizard.validateCurrentPage()
    assert declarations == ["unencrypted"]
    assert wizard._capability_label.text() == strings.WIZARD_CAPABILITY_DEGRADED
    assert wizard._root_status.text() == strings.WIZARD_CAPABILITY_DEGRADED_DESCRIPTION
