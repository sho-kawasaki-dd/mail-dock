"""Tree model for mail accounts and folders.

The model accepts a list of root nodes so another archive type can be added
without changing the model's hierarchy handling.  Account and folder records
are converted to nodes by :func:`build_mail_account_root`; the model itself
does not access repositories or infrastructure objects.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from PySide6.QtCore import (
    QAbstractItemModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    Qt,
)

from mail_dock.domain.search import MessageFilter
from mail_dock.presentation import strings

FolderTreeNodeKind = Literal["root", "all_accounts", "account", "folder", "custom"]
_EMPTY_INDEX = QModelIndex()


@dataclass(frozen=True)
class FolderTreeNode:
    """Immutable input node used to build the tree model.

    ``message_filter`` is available for future roots such as a PST archive.
    Mail-account nodes derive their filter from ``kind`` and the account or
    folder identifiers, so callers do not need to duplicate that logic.
    """

    key: str
    display_name: str
    kind: FolderTreeNodeKind
    account_id: str | None = None
    folder_id: int | None = None
    is_sync_target: bool | None = None
    children: tuple[FolderTreeNode, ...] = ()
    message_filter: MessageFilter | None = None


@dataclass
class _TreeItem:
    value: FolderTreeNode
    parent: _TreeItem | None = None
    children: list[_TreeItem] = field(default_factory=list)


def build_mail_account_root(
    accounts: Sequence[Mapping[str, object]],
    folders: Sequence[Mapping[str, object]],
) -> FolderTreeNode:
    """Build the standard mail-account root from repository-shaped records.

    Folder order follows the supplied records.  The ``display_name`` fields
    are used verbatim; raw names are only used as a stable fallback key.
    Folders without a matching account are omitted because they cannot yield a
    valid account/folder filter.
    """

    account_nodes: list[FolderTreeNode] = []
    folders_by_account: dict[str, list[FolderTreeNode]] = {}
    account_ids = {
        str(account_id) for account in accounts if (account_id := _account_id(account)) is not None
    }

    for folder in folders:
        account_id = _account_id(folder)
        folder_id = _folder_id(folder)
        if account_id is None or folder_id is None or account_id not in account_ids:
            continue
        display_name = _display_name(folder, fallback=f"folder-{folder_id}")
        folders_by_account.setdefault(account_id, []).append(
            FolderTreeNode(
                key=f"folder:{folder_id}",
                display_name=display_name,
                kind="folder",
                account_id=account_id,
                folder_id=folder_id,
                is_sync_target=bool(folder.get("is_sync_target", False)),
                message_filter=MessageFilter(account_ids=(account_id,), folder_ids=(folder_id,)),
            )
        )

    for account in accounts:
        account_id = _account_id(account)
        if account_id is None:
            continue
        account_name = _display_name(account, fallback=account_id)
        account_nodes.append(
            FolderTreeNode(
                key=f"account:{account_id}",
                display_name=account_name,
                kind="account",
                account_id=account_id,
                children=tuple(folders_by_account.get(account_id, ())),
                message_filter=MessageFilter(account_ids=(account_id,)),
            )
        )

    all_accounts = FolderTreeNode(
        key="all-accounts",
        display_name=strings.FILTER_ALL_ACCOUNTS,
        kind="all_accounts",
        message_filter=MessageFilter(),
    )
    return FolderTreeNode(
        key="mail-accounts",
        display_name=strings.TREE_ROOT_MAIL_ACCOUNTS,
        kind="root",
        children=(all_accounts, *account_nodes),
        message_filter=MessageFilter(),
    )


def build_mail_account_roots(
    accounts: Sequence[Mapping[str, object]],
    folders: Sequence[Mapping[str, object]],
) -> tuple[FolderTreeNode, ...]:
    """Return the mail-account root in the model's extensible root format."""

    return (build_mail_account_root(accounts, folders),)


class FolderTreeModel(QAbstractItemModel):
    """Display one or more archive roots and convert selections to filters."""

    NodeRole = int(Qt.ItemDataRole.UserRole)
    FilterRole = NodeRole + 1
    SyncTargetRole = NodeRole + 2

    def __init__(
        self,
        roots: Sequence[FolderTreeNode] = (),
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._roots: list[_TreeItem] = []
        self.set_roots(roots)

    def set_roots(self, roots: Sequence[FolderTreeNode]) -> None:
        """Replace all archive roots and reset the model."""

        self.beginResetModel()
        self._roots = [_materialize(root) for root in roots]
        self.endResetModel()

    def roots(self) -> tuple[FolderTreeNode, ...]:
        """Return the immutable root-node values currently displayed."""

        return tuple(item.value for item in self._roots)

    def index(
        self,
        row: int,
        column: int,
        parent: QModelIndex | QPersistentModelIndex = _EMPTY_INDEX,
    ) -> QModelIndex:
        if column != 0 or row < 0:
            return QModelIndex()
        children = self._children_for(parent)
        if row >= len(children):
            return QModelIndex()
        return self.createIndex(row, column, children[row])

    def parent(  # type: ignore[override]  # PySide6 combines QObject.parent overloads here.
        self,
        index: QModelIndex | QPersistentModelIndex,
    ) -> QModelIndex:
        if not index.isValid():
            return QModelIndex()
        item = self._item(index)
        if item is None or item.parent is None:
            return QModelIndex()
        parent_item = item.parent
        if parent_item.parent is None:
            return QModelIndex()
        return self.createIndex(
            parent_item.parent.children.index(parent_item),
            0,
            parent_item,
        )

    def rowCount(  # noqa: N802
        self,
        parent: QModelIndex | QPersistentModelIndex = _EMPTY_INDEX,
    ) -> int:
        return len(self._children_for(parent))

    def columnCount(  # noqa: N802
        self,
        parent: QModelIndex | QPersistentModelIndex = _EMPTY_INDEX,
    ) -> int:
        del parent
        return 1

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        item = self._item(index)
        if item is None:
            return None
        node = item.value
        if role == Qt.ItemDataRole.DisplayRole:
            return node.display_name
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip(node)
        if role == self.NodeRole:
            return node
        if role == self.FilterRole:
            return self.message_filter(index)
        if role == self.SyncTargetRole:
            return node.is_sync_target
        if role == Qt.ItemDataRole.CheckStateRole and node.kind == "folder":
            return Qt.CheckState.Checked if node.is_sync_target else Qt.CheckState.Unchecked
        return None

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def hasChildren(  # noqa: N802
        self,
        parent: QModelIndex | QPersistentModelIndex = _EMPTY_INDEX,
    ) -> bool:
        return bool(self._children_for(parent))

    def message_filter(
        self,
        index: QModelIndex | QPersistentModelIndex,
    ) -> MessageFilter | None:
        """Return the message filter represented by ``index``."""

        item = self._item(index)
        if item is None:
            return None
        node = item.value
        if node.message_filter is not None:
            return node.message_filter
        if node.kind == "account" and node.account_id is not None:
            return MessageFilter(account_ids=(node.account_id,))
        if node.kind == "folder" and node.account_id is not None and node.folder_id is not None:
            return MessageFilter(account_ids=(node.account_id,), folder_ids=(node.folder_id,))
        if node.kind in {"root", "all_accounts"}:
            return MessageFilter()
        return None

    def filter_for_index(self, index: QModelIndex) -> MessageFilter | None:
        """Alias that makes selection-to-filter call sites self-documenting."""

        return self.message_filter(index)

    def index_for_key(self, key: str) -> QModelIndex:
        """Return the index for the node with ``key``, or an invalid index.

        Used to restore or default a tree selection after ``set_roots``
        resets the view's expansion and selection state.
        """

        def _search(items: list[_TreeItem]) -> QModelIndex:
            for row, item in enumerate(items):
                if item.value.key == key:
                    return self.createIndex(row, 0, item)
                found = _search(item.children)
                if found.isValid():
                    return found
            return _EMPTY_INDEX

        return _search(self._roots)

    def _children_for(
        self,
        parent: QModelIndex | QPersistentModelIndex,
    ) -> list[_TreeItem]:
        if not parent.isValid():
            return self._roots
        item = self._item(parent)
        return item.children if item is not None else []

    @staticmethod
    def _item(index: QModelIndex | QPersistentModelIndex) -> _TreeItem | None:
        if not index.isValid():
            return None
        item = index.internalPointer()
        return item if isinstance(item, _TreeItem) else None

    @staticmethod
    def _tooltip(node: FolderTreeNode) -> str | None:
        if node.kind == "folder" and node.is_sync_target is not None:
            return (
                strings.TREE_FOLDER_SYNC_TARGET
                if node.is_sync_target
                else strings.TREE_FOLDER_NOT_SYNC_TARGET
            )
        return None


def _materialize(value: FolderTreeNode, parent: _TreeItem | None = None) -> _TreeItem:
    item = _TreeItem(value=value, parent=parent)
    item.children = [_materialize(child, item) for child in value.children]
    return item


def _account_id(record: Mapping[str, object]) -> str | None:
    account_id = record.get("account_id", record.get("id"))
    return _text_value(account_id)


def _folder_id(record: Mapping[str, object]) -> int | None:
    folder_id = record.get("id", record.get("folder_id"))
    if isinstance(folder_id, bool) or not isinstance(folder_id, int):
        return None
    return folder_id


def _display_name(record: Mapping[str, object], *, fallback: str) -> str:
    return _text_value(record.get("display_name")) or fallback


def _text_value(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
