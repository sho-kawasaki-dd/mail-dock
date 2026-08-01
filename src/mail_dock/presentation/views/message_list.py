"""Search controls for the message-list view."""

from __future__ import annotations

from datetime import UTC, datetime, time
from typing import cast

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QToolButton,
    QWidget,
)

from mail_dock.domain.search import MessageFilter
from mail_dock.presentation import strings
from mail_dock.presentation.viewmodels.message_list_viewmodel import (
    MessageListViewModel,
    SearchMode,
)


class MessageListSearchBar(QWidget):
    """Connect search controls to a :class:`MessageListViewModel`.

    Text changes only update ViewModel state. The request is issued by the
    line edit's ``returnPressed`` signal, so expensive LIKE searches are not
    started for every keystroke.
    """

    def __init__(self, viewmodel: MessageListViewModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._viewmodel = viewmodel
        self._build_controls()
        self._connect_controls()

    @property
    def search_input(self) -> QLineEdit:
        """Return the query editor for focus and integration tests."""

        return self._search_input

    @property
    def mode_selector(self) -> QComboBox:
        """Return the AND/OR selector."""

        return self._mode_selector

    @property
    def error_label(self) -> QLabel:
        """Return the inline query-error label."""

        return self._error_label

    @property
    def slow_path_label(self) -> QLabel:
        """Return the slow-path warning label."""

        return self._slow_path_label

    @property
    def cancel_button(self) -> QToolButton:
        """Return the search cancellation button."""

        return self._cancel_button

    def _build_controls(self) -> None:
        self._search_input = QLineEdit(self)
        self._search_input.setPlaceholderText(strings.SEARCH_PLACEHOLDER)

        self._mode_selector = QComboBox(self)
        self._mode_selector.addItem(strings.SEARCH_MODE_AND, "and")
        self._mode_selector.addItem(strings.SEARCH_MODE_OR, "or")
        self._mode_selector.setToolTip(strings.SEARCH_MODE_AND)

        self._clear_button = QToolButton(self)
        self._clear_button.setIcon(QIcon.fromTheme("edit-clear"))
        self._clear_button.setText(strings.SEARCH_CLEAR)
        self._clear_button.setToolTip(strings.SEARCH_CLEAR)

        self._from_enabled = QCheckBox(strings.SEARCH_DATE_FROM, self)
        self._from_date = QDateEdit(self)
        self._from_date.setCalendarPopup(True)
        self._from_date.setEnabled(False)

        self._to_enabled = QCheckBox(strings.SEARCH_DATE_TO, self)
        self._to_date = QDateEdit(self)
        self._to_date.setCalendarPopup(True)
        self._to_date.setEnabled(False)

        self._attachment_selector = QComboBox(self)
        self._attachment_selector.addItem(
            strings.SEARCH_ATTACHMENT_ALL,
            None,
        )
        self._attachment_selector.addItem(
            strings.SEARCH_ATTACHMENT_YES,
            True,
        )
        self._attachment_selector.addItem(
            strings.SEARCH_ATTACHMENT_NO,
            False,
        )
        self._attachment_selector.setToolTip(strings.SEARCH_ATTACHMENT_FILTER)

        self._error_label = QLabel(self)
        self._error_label.setStyleSheet("color: #b42318;")
        self._error_label.setVisible(False)

        self._slow_path_label = QLabel(strings.SEARCH_SLOW_PATH_WARNING, self)
        self._slow_path_label.setStyleSheet("color: #9a6700;")
        self._slow_path_label.setVisible(False)

        self._cancel_button = QToolButton(self)
        self._cancel_button.setText(strings.SEARCH_CANCEL)
        self._cancel_button.setToolTip(strings.SEARCH_CANCEL)
        self._cancel_button.setEnabled(False)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._search_input, 1)
        layout.addWidget(self._mode_selector)
        layout.addWidget(self._clear_button)
        layout.addWidget(self._from_enabled)
        layout.addWidget(self._from_date)
        layout.addWidget(self._to_enabled)
        layout.addWidget(self._to_date)
        layout.addWidget(self._attachment_selector)
        layout.addWidget(self._slow_path_label)
        layout.addWidget(self._error_label)
        layout.addWidget(self._cancel_button)

    def _connect_controls(self) -> None:
        self._search_input.textChanged.connect(self._viewmodel.set_search_query)
        self._search_input.returnPressed.connect(self._execute_search)
        self._mode_selector.currentIndexChanged.connect(self._mode_changed)
        self._clear_button.clicked.connect(self._clear_search)
        self._from_enabled.toggled.connect(self._from_date.setEnabled)
        self._to_enabled.toggled.connect(self._to_date.setEnabled)
        self._from_enabled.toggled.connect(self._structured_filter_changed)
        self._to_enabled.toggled.connect(self._structured_filter_changed)
        self._from_date.dateChanged.connect(self._structured_filter_changed)
        self._to_date.dateChanged.connect(self._structured_filter_changed)
        self._attachment_selector.currentIndexChanged.connect(self._structured_filter_changed)
        self._cancel_button.clicked.connect(self._viewmodel.cancel_search)
        self._viewmodel.search_error_changed.connect(self._show_search_error)
        self._viewmodel.slow_path_changed.connect(self._show_slow_path_warning)
        self._viewmodel.request_busy_changed.connect(self._set_request_busy)

    def _execute_search(self) -> None:
        self._viewmodel.execute_search()

    def _mode_changed(self, index: int) -> None:
        mode = cast(SearchMode, self._mode_selector.itemData(index))
        self._viewmodel.set_search_mode(mode)

    def _clear_search(self) -> None:
        self._search_input.clear()
        self._viewmodel.clear_search()

    def _structured_filter_changed(self) -> None:
        current = self._viewmodel.filters
        self._viewmodel.set_filters(
            MessageFilter(
                account_ids=current.account_ids,
                folder_ids=current.folder_ids,
                date_from=self._selected_date(self._from_enabled, self._from_date, start=True),
                date_to=self._selected_date(self._to_enabled, self._to_date, start=False),
                has_attachment=self._attachment_selector.currentData(),
                local_states=current.local_states,
                remote_states=current.remote_states,
                thread_key=current.thread_key,
            )
        )

    @staticmethod
    def _selected_date(
        enabled: QCheckBox,
        editor: QDateEdit,
        *,
        start: bool,
    ) -> datetime | None:
        if not enabled.isChecked():
            return None
        selected = editor.date()
        selected_date = selected.toPython()
        boundary = time.min if start else time.max
        return datetime.combine(selected_date, boundary, tzinfo=UTC)

    def _show_search_error(self, message: str) -> None:
        self._error_label.setText(message)
        self._error_label.setVisible(bool(message))

    def _show_slow_path_warning(self, visible: bool) -> None:
        self._slow_path_label.setVisible(visible)

    def _set_request_busy(self, busy: bool) -> None:
        self._cancel_button.setEnabled(busy)