"""Asynchronous message detail and HTML preview view."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from html import escape
from typing import Literal, Protocol, cast

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from mail_dock.domain.messages import RenderedMessage
from mail_dock.domain.search import MessageDetail, MessageFilter, MessageSummary
from mail_dock.presentation import strings
from mail_dock.presentation.errors import user_message
from mail_dock.usecases.open_message import OpenedMessage

from ..web.interceptor import MailUrlRequestInterceptor
from ..web.page import MailPage
from ..web.profile import create_mail_profile
from ..web.schemes import CidSchemeHandler, MailBodySchemeHandler

HtmlSanitizer = Callable[..., str]
DetailChannel = Literal["detail/open"]
ThreadChannel = Literal["count/thread"]


class _RequestHandle(Protocol):
    request_id: int


class _Signal(Protocol):
    def connect(self, slot: Callable[[object], None]) -> object:
        """Connect a Qt-like result signal."""


class _DetailQueryWorker(Protocol):
    result: _Signal
    request_failed: _Signal

    def open_message(self, *, message_id: int) -> _RequestHandle:
        """Queue an asynchronous message-open operation."""

    def list_thread(
        self,
        *,
        thread_key: str,
        filters: MessageFilter | None = None,
    ) -> _RequestHandle:
        """Queue an asynchronous thread-list operation."""

    def cancel(self, channel: str) -> _RequestHandle | None:
        """Cancel the current request in one independent channel."""


class DetailView(QWidget):
    """Show message metadata and verified EML content without blocking the UI.

    The worker, renderer output sanitizer, and WebEngine profile are all
    supplied through presentation composition. This view does not construct
    database or parsing infrastructure itself.
    """

    message_loaded = Signal(object)
    thread_loaded = Signal(object)
    error_changed = Signal(str)

    def __init__(
        self,
        worker: _DetailQueryWorker,
        sanitize_html: HtmlSanitizer,
        parent: QWidget | None = None,
        *,
        block_remote_images: bool = True,
    ) -> None:
        super().__init__(parent)
        self._worker = worker
        self._sanitize_html = sanitize_html
        self._block_remote_images = block_remote_images
        self._allow_remote_images_for_message = not block_remote_images
        self._current_summary: MessageSummary | None = None
        self._current_detail: MessageDetail | None = None
        self._current_rendered: RenderedMessage | None = None
        self._open_request_id: int | None = None
        self._thread_request_id: int | None = None

        self._interceptor = MailUrlRequestInterceptor()
        self._profile = create_mail_profile(
            self,
            request_interceptor=self._interceptor,
        )
        self._cid_handler = cast(
            CidSchemeHandler,
            self._profile.urlSchemeHandler(b"cid"),
        )
        self._body_handler = cast(
            MailBodySchemeHandler,
            self._profile.urlSchemeHandler(b"maildock"),
        )
        self._build_controls()
        self._connect_worker()

    @property
    def body_view(self) -> QWebEngineView:
        """Return the sandboxed HTML body view."""

        return self._body_view

    @property
    def attachment_list(self) -> QListWidget:
        """Return the non-inline attachment list."""

        return self._attachment_list

    @property
    def remote_images_button(self) -> QPushButton:
        """Return the per-message remote-image permission button."""

        return self._remote_images_button

    @property
    def thread_button(self) -> QPushButton:
        """Return the button that requests the current conversation."""

        return self._thread_button

    @property
    def state_label(self) -> QLabel:
        """Return the body state or error label."""

        return self._state_label

    def show_message(self, summary: MessageSummary | None) -> None:
        """Display ``summary`` and asynchronously load its verified EML."""
        self._cancel_request("detail/open", self._open_request_id)
        self._open_request_id = None
        self._cancel_request("count/thread", self._thread_request_id)
        self._thread_request_id = None
        self._current_summary = summary
        self._current_detail = None
        self._current_rendered = None
        self._allow_remote_images_for_message = not self._block_remote_images
        self._reset_display()

        if summary is None:
            return

        self._set_header(summary)
        if summary.local_state == "purged":
            self._show_state(strings.DETAIL_PURGED)
            return
        if summary.failure_class == "oversize":
            self._show_state(strings.STATUS_OVERSIZE)
            return

        self._set_loading(True)
        handle = self._worker.open_message(message_id=summary.id)
        self._open_request_id = handle.request_id

    load_message = show_message

    def clear_message(self) -> None:
        """Clear the preview and cancel outstanding detail requests."""
        self.show_message(None)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        """Detach the page before the off-the-record profile is destroyed."""
        self._body_view.setPage(None)  # type: ignore[arg-type]  # Qt accepts None to detach the page.
        self._page.deleteLater()
        self._profile.deleteLater()
        super().closeEvent(event)

    def show_thread(self) -> None:
        """Request the current message's conversation through ``QueryWorker``."""
        detail = self._current_detail or self._current_summary
        thread_key = detail.thread_key if detail is not None else None
        if not thread_key:
            return
        if self._thread_request_id is not None:
            self._worker.cancel("count/thread")
        self._thread_button.setEnabled(False)
        handle = self._worker.list_thread(thread_key=thread_key)
        self._thread_request_id = handle.request_id

    request_thread = show_thread

    def _build_controls(self) -> None:
        self._subject_label = QLabel(self)
        self._subject_label.setWordWrap(True)
        self._subject_label.setStyleSheet("font-weight: bold; font-size: 14px;")

        self._header_values: dict[str, QLabel] = {}
        header_form = QFormLayout()
        header_form.addRow(strings.DETAIL_HEADER_SUBJECT, self._subject_label)
        for key, label in (
            ("sender", strings.DETAIL_HEADER_FROM),
            ("recipient", strings.DETAIL_HEADER_TO),
            ("cc", strings.DETAIL_HEADER_CC),
            ("date", strings.DETAIL_HEADER_DATE),
            ("account", strings.DETAIL_HEADER_ACCOUNT),
            ("folder", strings.DETAIL_HEADER_FOLDER),
        ):
            value = QLabel(self)
            value.setWordWrap(True)
            self._header_values[key] = value
            header_form.addRow(label, value)

        self._remote_images_label = QLabel(strings.DETAIL_REMOTE_IMAGES_BLOCKED, self)
        self._remote_images_button = QPushButton(strings.DETAIL_LOAD_REMOTE_IMAGES, self)
        self._remote_images_button.clicked.connect(self._allow_remote_images)
        remote_layout = QHBoxLayout()
        remote_layout.addWidget(self._remote_images_label)
        remote_layout.addWidget(self._remote_images_button)
        remote_layout.addStretch(1)
        self._remote_images_banner = QFrame(self)
        self._remote_images_banner.setLayout(remote_layout)
        self._remote_images_banner.setVisible(False)

        self._thread_button = QPushButton(self)
        self._thread_button.clicked.connect(self.show_thread)
        self._thread_button.setVisible(False)

        self._state_label = QLabel(self)
        self._state_label.setWordWrap(True)
        self._state_label.setVisible(False)

        self._body_view = QWebEngineView(self)
        self._page = MailPage(
            self._profile,
            self,
            dialog_parent=self,
        )
        self._body_view.setPage(self._page)
        self._body_stack = QStackedWidget(self)
        self._body_stack.addWidget(self._state_label)
        self._body_stack.addWidget(self._body_view)
        self._body_stack.setCurrentWidget(self._state_label)

        self._attachment_list = QListWidget(self)
        self._attachment_list.setMinimumHeight(80)
        attachment_box = QGroupBox(strings.DETAIL_ATTACHMENT_LIST, self)
        attachment_layout = QVBoxLayout(attachment_box)
        attachment_layout.addWidget(self._attachment_list)

        layout = QVBoxLayout(self)
        layout.addLayout(header_form)
        layout.addWidget(self._remote_images_banner)
        layout.addWidget(self._thread_button)
        layout.addWidget(self._body_stack, 1)
        layout.addWidget(attachment_box)

    def _connect_worker(self) -> None:
        self._worker.result.connect(self._on_result)
        self._worker.request_failed.connect(self._on_failure)

    def _reset_display(self) -> None:
        self._interceptor.reset_for_message()
        self._cid_handler.clear_message()
        self._body_handler.clear_message()
        self._page.reset_body_navigation()
        self._subject_label.clear()
        for value in self._header_values.values():
            value.clear()
        self._thread_button.setVisible(False)
        self._thread_button.setEnabled(True)
        self._remote_images_banner.setVisible(False)
        self._state_label.clear()
        self._state_label.setVisible(False)
        self._body_stack.setCurrentWidget(self._state_label)
        self._attachment_list.clear()
        self._set_error("")

    def _set_header(self, message: MessageSummary | MessageDetail) -> None:
        self._subject_label.setText(message.subject)
        self._header_values["sender"].setText(message.sender)
        self._header_values["recipient"].setText(getattr(message, "recipient", ""))
        self._header_values["cc"].setText(getattr(message, "cc", ""))
        self._header_values["date"].setText(
            _format_date(message.date_sent or message.internal_date)
        )
        self._header_values["account"].setText(message.account_id)
        self._header_values["folder"].setText(message.folder_display_name)
        if message.thread_key:
            self._thread_button.setText(strings.DETAIL_THREAD_SHOW.format(count=1))
            self._thread_button.setVisible(True)

    def _on_result(self, result: object) -> None:
        channel = getattr(result, "channel", None)
        request_id = getattr(result, "request_id", None)
        value = getattr(result, "value", None)
        if channel == "detail/open" and request_id == self._open_request_id:
            self._open_request_id = None
            if not isinstance(value, OpenedMessage):
                self._show_state(strings.ERROR_UNKNOWN)
                return
            self._current_detail = value.detail
            self._set_header(value.detail)
            self._current_rendered = value.rendered
            self._set_loading(False)
            self._display_rendered(value.rendered)
            self.message_loaded.emit(value)
            return

        if channel == "count/thread" and request_id == self._thread_request_id:
            self._thread_request_id = None
            self._thread_button.setEnabled(True)
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
                return
            items = tuple(item for item in value if isinstance(item, MessageSummary))
            if len(items) != len(value):
                return
            self._thread_button.setText(strings.DETAIL_THREAD_SHOW.format(count=len(items)))
            self.thread_loaded.emit(items)

    def _on_failure(self, failure: object) -> None:
        channel = getattr(failure, "channel", None)
        request_id = getattr(failure, "request_id", None)
        error = getattr(failure, "error", failure)
        message = user_message(cast(BaseException, error))
        if channel == "detail/open" and request_id == self._open_request_id:
            self._open_request_id = None
            self._set_loading(False)
            self._show_state(message)
            self._set_error(message)
        elif channel == "count/thread" and request_id == self._thread_request_id:
            self._thread_request_id = None
            self._thread_button.setEnabled(True)
            self._set_error(message)

    def _display_rendered(self, rendered: RenderedMessage) -> None:
        allow_remote_images = self._allow_remote_images_for_message
        self._interceptor.set_allow_remote_images(allow_remote_images)
        html_body = rendered.html_body
        if html_body is None and rendered.text_body:
            html_body = f"<pre>{escape(rendered.text_body)}</pre>"
        if not html_body:
            self._show_state(strings.DETAIL_NO_BODY)
            self._populate_attachments(rendered)
            return

        sanitized = self._sanitize_html(
            html_body,
            allow_remote_images=allow_remote_images,
        )
        display_message = RenderedMessage(
            html_body=sanitized,
            text_body=rendered.text_body,
            parts=rendered.parts,
        )
        self._cid_handler.set_message(rendered)
        self._body_handler.set_message(display_message)
        self._page.reset_body_navigation()
        self._body_view.setUrl(QUrl("maildock:/body"))
        self._body_stack.setCurrentWidget(self._body_view)
        self._remote_images_banner.setVisible(not allow_remote_images)
        self._populate_attachments(rendered)

    def _allow_remote_images(self) -> None:
        if self._current_rendered is None:
            return
        self._allow_remote_images_for_message = True
        self._display_rendered(self._current_rendered)

    def _populate_attachments(self, rendered: RenderedMessage) -> None:
        self._attachment_list.clear()
        for part_index, part in enumerate(rendered.parts):
            if part.is_inline:
                continue
            filename = part.filename or part.content_type
            item = QListWidgetItem(
                f"{filename} ({_format_size(len(part.payload))}, {part.content_type})"
            )
            item.setData(256, part_index)
            self._attachment_list.addItem(item)

    def _show_state(self, message: str) -> None:
        self._state_label.setText(message)
        self._state_label.setVisible(True)
        self._body_stack.setCurrentWidget(self._state_label)
        self._remote_images_banner.setVisible(False)

    def _set_loading(self, loading: bool) -> None:
        if loading:
            self._show_state(strings.STATUS_LOADING)

    def _set_error(self, message: str) -> None:
        if message:
            self.error_changed.emit(message)

    def _cancel_request(self, channel: str, request_id: int | None) -> None:
        if request_id is not None:
            self._worker.cancel(channel)


def _format_date(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.strftime("%Y-%m-%d %H:%M")


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"
