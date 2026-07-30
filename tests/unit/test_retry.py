from threading import Event

import pytest

from mail_dock.domain.errors import (
    AuthenticationError,
    OperationCancelledError,
    OversizeError,
    PermanentError,
    TransientError,
    UidValidityChanged,
)
from mail_dock.domain.fetcher import CancelToken
from mail_dock.usecases.retry import with_retry


def test_with_retry_uses_exponential_backoff_and_retries_transient_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    delays: list[float] = []

    def operation() -> str:
        nonlocal calls
        calls += 1
        raise TransientError("temporary")

    def record_wait(_event: Event, timeout: float) -> bool:
        delays.append(timeout)
        return False

    monkeypatch.setattr("mail_dock.usecases.retry.random.uniform", lambda lower, upper: 0.0)
    monkeypatch.setattr(Event, "wait", record_wait)

    with pytest.raises(TransientError):
        with_retry(operation)

    assert calls == 4
    assert delays == [1.0, 2.0, 4.0]


@pytest.mark.parametrize(
    "error",
    [
        AuthenticationError("auth"),
        PermanentError("permanent"),
        OversizeError("oversize"),
        UidValidityChanged("uidvalidity changed"),
    ],
)
def test_with_retry_does_not_retry_non_transient_errors(error: Exception) -> None:
    calls = 0

    def operation() -> None:
        nonlocal calls
        calls += 1
        raise error

    with pytest.raises(type(error)):
        with_retry(operation)

    assert calls == 1


def test_with_retry_cancellation_interrupts_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    cancel = CancelToken()

    def cancel_during_wait(_event: Event, _timeout: float) -> bool:
        cancel.cancel()
        return True

    monkeypatch.setattr(Event, "wait", cancel_during_wait)

    with pytest.raises(OperationCancelledError):
        with_retry(lambda: (_ for _ in ()).throw(TransientError("temporary")), cancel=cancel)
