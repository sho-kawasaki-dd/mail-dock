from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

import pytest
from PySide6.QtCore import QObject, Signal

from mail_dock.domain.messages import MessagePart, RenderedMessage
from mail_dock.domain.search import MessageDetail, MessageFilter, MessageSummary
from mail_dock.presentation import strings
from mail_dock.presentation.views.detail_view import DetailView
from mail_dock.usecases.open_message import OpenedMessage

pytestmark = pytest.mark.gui


@dataclass(frozen=True)
class _Request:
    request_id: int


class _FakeWorker(QObject):
    result = Signal(object)
    request_failed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._next_request_id = 0
        self.opened: list[int] = []
        self.cancelled: list[str] = []

    def open_message(self, *, message_id: int) -> _Request:
        self._next_request_id += 1
        self.opened.append(message_id)
        return _Request(self._next_request_id)

    def list_thread(
        self,
        *,
        thread_key: str,
        filters: MessageFilter | None = None,
    ) -> _Request:
        del thread_key, filters
        self._next_request_id += 1
        return _Request(self._next_request_id)

    def cancel(self, channel: Literal["detail/open", "count/thread"]) -> _Request | None:
        self.cancelled.append(channel)
        return None


def _summary(*, message_id: int = 1, local_state: str = "active") -> MessageSummary:
    return MessageSummary(
        id=message_id,
        account_id="account-1",
        folder_id=10,
        folder_raw_name="INBOX",
        folder_display_name="受信箱",
        subject="件名",
        sender="sender@example.com",
        date_sent=datetime(2026, 1, 2, tzinfo=UTC),
        internal_date=None,
        size_bytes=4,
        has_attachment=True,
        remote_state="present",
        local_state=local_state,
        thread_key="thread-1",
        imap_flags="\\Seen",
        moved_to_folder_display_name=None,
        failure_class=None,
    )


def _detail(summary: MessageSummary) -> MessageDetail:
    return MessageDetail(
        **summary.__dict__,
        recipient="recipient@example.com",
        cc="",
        message_id="<message@example.com>",
        in_reply_to=None,
        references_ids=None,
        relative_path="eml/message.eml",
        file_hash="a" * 64,
    )


def test_purged_message_uses_fallback_without_opening_eml(qtbot: Any) -> None:
    worker = _FakeWorker()
    view = DetailView(cast(Any, worker), lambda html, **_kwargs: html)
    qtbot.addWidget(view)
    view.show()
    view.show_message(_summary(local_state="purged"))

    assert worker.opened == []
    assert view.state_label.isVisible()
    assert view.state_label.text() == strings.DETAIL_PURGED


def test_rendered_attachments_exclude_inline_parts(qtbot: Any) -> None:
    worker = _FakeWorker()
    view = DetailView(cast(Any, worker), lambda html, **_kwargs: html)
    qtbot.addWidget(view)
    summary = _summary()
    view.show_message(summary)
    request = _Request(worker._next_request_id)
    rendered = RenderedMessage(
        html_body=None,
        text_body="本文",
        parts=(
            MessagePart("inline", "image/png", "inline.png", b"x", True),
            MessagePart(None, "application/pdf", "report.pdf", b"pdf", False),
        ),
    )
    worker.result.emit(
        type(
            "Result",
            (),
            {
                "channel": "detail/open",
                "request_id": request.request_id,
                "value": OpenedMessage(_detail(summary), rendered),
            },
        )()
    )

    assert worker.opened == [summary.id]
    assert view.current_detail is not None
    assert view.attachment_list.count() == 1
    assert view.attachment_list.item(0).text().startswith("report.pdf")


def test_thread_button_updates_count_and_emits_loaded_items(qtbot: Any) -> None:
    worker = _FakeWorker()
    view = DetailView(cast(Any, worker), lambda html, **_kwargs: html)
    qtbot.addWidget(view)
    view.show()
    summary = _summary()
    view.show_message(summary)
    assert view.thread_button.isVisible()
    assert "1" in view.thread_button.text()

    view.show_thread()
    request = _Request(worker._next_request_id)
    items = (_summary(message_id=1), _summary(message_id=2))
    received: list[object] = []
    view.thread_loaded.connect(received.append)
    worker.result.emit(
        type(
            "Result",
            (),
            {"channel": "count/thread", "request_id": request.request_id, "value": items},
        )()
    )

    assert "2" in view.thread_button.text()
    assert received == [items]


def _open(worker: _FakeWorker, view: DetailView, summary: MessageSummary, html_body: str) -> None:
    view.show_message(summary)
    request = _Request(worker._next_request_id)
    rendered = RenderedMessage(html_body=html_body, text_body="", parts=())
    worker.result.emit(
        type(
            "Result",
            (),
            {
                "channel": "detail/open",
                "request_id": request.request_id,
                "value": OpenedMessage(_detail(summary), rendered),
            },
        )()
    )


def test_remote_images_banner_hidden_without_remote_image_references(qtbot: Any) -> None:
    worker = _FakeWorker()
    view = DetailView(cast(Any, worker), lambda html, **_kwargs: html)
    qtbot.addWidget(view)
    view.show()
    _open(worker, view, _summary(), "<p>本文のみ、画像なし</p>")

    parent = view.remote_images_button.parentWidget()
    assert parent is not None
    assert not parent.isVisible()


def test_remote_images_banner_shown_for_remote_image_reference(qtbot: Any) -> None:
    worker = _FakeWorker()
    view = DetailView(cast(Any, worker), lambda html, **_kwargs: html)
    qtbot.addWidget(view)
    view.show()
    _open(worker, view, _summary(), '<img src="https://example.test/pixel.gif">')

    parent = view.remote_images_button.parentWidget()
    assert parent is not None
    assert parent.isVisible()

    view.remote_images_button.click()
    assert not parent.isVisible()
