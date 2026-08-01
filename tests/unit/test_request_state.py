from mail_dock.presentation.threads.request_state import RequestState


def test_new_request_cancels_only_the_previous_same_channel() -> None:
    state = RequestState()

    old_list = state.issue("list/search")
    old_detail = state.issue("detail/open")
    new_list = state.issue("list/search")

    assert old_list.token.is_cancelled
    assert not old_detail.token.is_cancelled
    assert not new_list.token.is_cancelled
    assert not state.is_current("list/search", old_list.request_id)
    assert state.is_current("list/search", new_list.request_id)
    assert state.is_current("detail/open", old_detail.request_id)


def test_cancel_and_finish_preserve_channel_generations() -> None:
    state = RequestState()
    request = state.issue("count/thread")

    assert state.cancel("count/thread") == request
    assert request.token.is_cancelled
    assert state.is_current("count/thread", request.request_id)

    state.finish(request)
    assert state.current("count/thread") is None
    assert not state.is_current("count/thread", request.request_id)