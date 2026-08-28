from __future__ import annotations

from typing import Any

import pytest
from PySide6.QtCore import QObject, Signal

from mail_dock.presentation import strings
from mail_dock.presentation.views.dialogs.audit_log_dialog import AuditLogDialog

pytestmark = pytest.mark.gui


class _Worker(QObject):
    audit_log_result = Signal(object)
    error_reported = Signal(object)

    def __init__(self, pages: dict[int, tuple[dict[str, Any], ...]]) -> None:
        super().__init__()
        self._pages = pages
        self.calls: list[tuple[int, int]] = []

    def list_audit_log(self, limit: int, offset: int) -> object:
        self.calls.append((limit, offset))
        self.audit_log_result.emit(self._pages.get(offset, ()))
        return object()


def _entry(index: int, *, operation: str = "local_purge") -> dict[str, Any]:
    return {
        "occurred_at": f"2026-08-2{index}T00:00:00Z",
        "operation": operation,
        "account_id": f"user{index}@example.com",
        "message_id": f"<msg-{index}@example.com>",
        "subject": "件名は20文字を超えると省略されてマスクされる想定です",
        "size_bytes": 1024 * index,
        "detail": f"contact user{index}@example.com for details",
    }


def test_audit_log_dialog_is_read_only_and_masks_subject_and_emails(qtbot: Any) -> None:
    entries = tuple(_entry(index) for index in range(1, 51))
    worker = _Worker({0: entries})
    dialog = AuditLogDialog(worker)
    qtbot.addWidget(dialog)

    assert worker.calls == [(50, 0)]
    assert dialog.table.rowCount() == 50
    assert dialog.table.editTriggers() == dialog.table.EditTrigger.NoEditTriggers

    subject_item = dialog.table.item(0, 4)
    assert subject_item is not None
    assert subject_item.text().endswith("...")
    assert len(subject_item.text()) == 23

    account_item = dialog.table.item(0, 2)
    assert account_item is not None
    assert "user1@example.com" not in account_item.text()
    assert "@example.com" in account_item.text()

    detail_item = dialog.table.item(0, 6)
    assert detail_item is not None
    assert "user1@example.com" not in detail_item.text()

    assert dialog.previous_button.isEnabled() is False
    assert dialog.next_button.isEnabled() is True


def test_audit_log_dialog_paginates_forward_and_back(qtbot: Any) -> None:
    first_page = tuple(_entry(index) for index in range(1, 51))
    second_page = tuple(_entry(index) for index in range(51, 56))
    worker = _Worker({0: first_page, 50: second_page})
    dialog = AuditLogDialog(worker)
    qtbot.addWidget(dialog)

    dialog.next_button.click()
    assert worker.calls[-1] == (50, 50)
    assert dialog.table.rowCount() == 5
    assert dialog.previous_button.isEnabled() is True
    assert dialog.next_button.isEnabled() is False

    dialog.previous_button.click()
    assert worker.calls[-1] == (50, 0)
    assert dialog.table.rowCount() == 50


def test_audit_log_dialog_shows_empty_status_with_no_entries(qtbot: Any) -> None:
    worker = _Worker({0: ()})
    dialog = AuditLogDialog(worker)
    qtbot.addWidget(dialog)

    assert dialog.status_label.text() == strings.AUDIT_LOG_EMPTY
    assert dialog.table.rowCount() == 0
