from __future__ import annotations

import pytest

from mail_dock.presentation.threads.request_state import RequestState


def test_new_generation_cancels_only_the_previous_request_in_one_channel() -> None:
    state = RequestState()

    old_list = state.issue("list/search")
    detail = state.issue("detail/open")
    count = state.issue("count/thread")
    new_list = state.issue("list/search")

    assert old_list.token.is_cancelled
    assert not detail.token.is_cancelled
    assert not count.token.is_cancelled
    assert not new_list.token.is_cancelled
    assert not state.is_current("list/search", old_list.request_id)
    assert state.is_current("list/search", new_list.request_id)
    assert state.is_current("detail/open", detail.request_id)
    assert state.is_current("count/thread", count.request_id)


def test_ui_can_cancel_running_token_without_invalidating_other_channels() -> None:
    state = RequestState()
    detail = state.issue("detail/open")
    count = state.issue("count/thread")

    cancelled = state.cancel("detail/open")

    assert cancelled is detail
    assert detail.token.is_cancelled
    assert not count.token.is_cancelled
    assert state.is_current("detail/open", detail.request_id)
    assert state.is_current("count/thread", count.request_id)


def test_finished_old_request_cannot_clear_a_newer_generation() -> None:
    state = RequestState()
    old_request = state.issue("list/search")
    new_request = state.issue("list/search")

    state.finish(old_request)

    assert state.current("list/search") is new_request
    assert state.is_current("list/search", new_request.request_id)


def test_unknown_request_channel_is_rejected() -> None:
    state = RequestState()

    with pytest.raises(ValueError, match="unknown request channel"):
        state.issue("unknown")  # type: ignore[arg-type]