from __future__ import annotations

import pytest
from PySide6.QtCore import QModelIndex, Qt

from mail_dock.domain.search import MessageFilter
from mail_dock.presentation import strings
from mail_dock.presentation.models.folder_tree_model import (
    FolderTreeModel,
    build_mail_account_roots,
    build_pst_archive_roots,
)

pytestmark = pytest.mark.gui


def _model() -> FolderTreeModel:
    return FolderTreeModel(
        build_mail_account_roots(
            [
                {"id": "account-1", "display_name": "仕事"},
                {"id": "account-2", "display_name": "個人"},
            ],
            [
                {
                    "id": 10,
                    "account_id": "account-1",
                    "raw_name": "INBOX",
                    "display_name": "受信箱",
                    "is_sync_target": 1,
                },
                {
                    "id": 11,
                    "account_id": "account-1",
                    "raw_name": "Archive",
                    "display_name": "アーカイブ",
                    "is_sync_target": 0,
                },
            ],
        )
    )


def test_tree_hierarchy_and_selected_filters(qtbot: object) -> None:
    del qtbot
    model = _model()
    root = model.index(0, 0)
    all_accounts = model.index(0, 0, root)
    account = model.index(1, 0, root)
    folder = model.index(0, 0, account)

    assert model.data(root) == strings.TREE_ROOT_MAIL_ACCOUNTS
    assert model.data(all_accounts) == strings.FILTER_ALL_ACCOUNTS
    assert model.data(account) == "仕事"
    assert model.data(folder) == "受信箱"
    trash = model.index(3, 0, root)
    assert model.data(trash) == strings.TREE_LOCAL_TRASH
    assert model.rowCount(root) == 4
    assert model.rowCount(account) == 2
    assert model.filter_for_index(all_accounts) == MessageFilter()
    assert model.filter_for_index(account) == MessageFilter(account_ids=("account-1",))
    assert model.filter_for_index(folder) == MessageFilter(
        account_ids=("account-1",), folder_ids=(10,)
    )
    assert model.filter_for_index(trash) == MessageFilter(local_states=frozenset({"trashed"}))


def test_folder_sync_target_is_metadata_not_an_editable_state(qtbot: object) -> None:
    del qtbot
    model = _model()
    account = model.index(1, 0, model.index(0, 0))
    target = model.index(0, 0, account)
    non_target = model.index(1, 0, account)

    assert model.data(target, model.SyncTargetRole) is True
    assert model.data(non_target, model.SyncTargetRole) is False
    assert model.data(target, Qt.ItemDataRole.ToolTipRole) == strings.TREE_FOLDER_SYNC_TARGET
    assert model.flags(target) == Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
    assert model.data(target, Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked


def test_model_replaces_extensible_roots(qtbot: object) -> None:
    del qtbot
    model = _model()
    custom_root = model.roots()[0]

    model.set_roots((custom_root, custom_root))

    assert model.rowCount(QModelIndex()) == 2
    assert not model.parent(model.index(0, 0)).isValid()


def test_pst_tree_shows_only_active_completed_generations(qtbot: object) -> None:
    del qtbot
    model = FolderTreeModel(
        build_pst_archive_roots(
            [
                {
                    "id": 1,
                    "account_id": "pst-active",
                    "display_name": "現行PST",
                    "status": "completed",
                    "is_active": 1,
                },
                {
                    "id": 2,
                    "account_id": "pst-old",
                    "display_name": "旧PST",
                    "status": "superseded",
                    "is_active": 0,
                },
                {
                    "id": 3,
                    "account_id": "pst-running",
                    "display_name": "処理中PST",
                    "status": "ingesting",
                    "is_active": 0,
                },
            ],
            [
                {
                    "id": 20,
                    "account_id": "pst-active",
                    "display_name": "受信箱",
                },
                {
                    "id": 21,
                    "account_id": "pst-old",
                    "display_name": "旧受信箱",
                },
            ],
        )
    )
    root = model.index(0, 0)

    assert model.data(root) == strings.TREE_ROOT_PST_ARCHIVES
    assert model.rowCount(root) == 3
    archive = model.index(1, 0, root)
    folder = model.index(0, 0, archive)
    assert model.data(archive) == "現行PST"
    assert model.data(folder) == "受信箱"
    assert model.data(model.index(2, 0, root)) == strings.TREE_PST_TRASH
    assert model.filter_for_index(folder) == MessageFilter(
        account_ids=("pst-active",), folder_ids=(20,)
    )


def test_pst_tree_parent_round_trip_for_archive_folder(qtbot: object) -> None:
    del qtbot
    model = FolderTreeModel(
        build_pst_archive_roots(
            [
                {
                    "id": 7,
                    "account_id": "pst-7",
                    "display_name": "アーカイブ",
                    "status": "completed_with_errors",
                    "is_active": True,
                }
            ],
            [{"id": 70, "account_id": "pst-7", "display_name": "Inbox"}],
        )
    )
    archive = model.index(1, 0, model.index(0, 0))
    folder = model.index(0, 0, archive)

    assert model.parent(folder) == archive
    assert model.index_for_key("pst-folder:70") == folder
