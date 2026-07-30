"""Retry policy shared by mail-dock use cases."""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from threading import Event

from mail_dock.domain.errors import TransientError
from mail_dock.domain.fetcher import CancelToken

LOGGER = logging.getLogger(__name__)


def with_retry[ResultT](
    fn: Callable[[], ResultT],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    cancel: CancelToken | None = None,
) -> ResultT:
    """Call ``fn`` and retry only transient failures with exponential backoff.

    ``attempts`` is the maximum number of retries after the initial call. Each
    retry waits for ``base_delay * 2 ** retry_number`` seconds, plus up to ten
    percent jitter. A cancellation token interrupts the wait and is checked
    before every call.
    """
    if attempts < 0:
        raise ValueError("attempts must be non-negative")
    if base_delay < 0:
        raise ValueError("base_delay must be non-negative")

    wait_event = cancel.event if cancel is not None else Event()

    for retry_number in range(attempts + 1):
        if cancel is not None:
            cancel.raise_if_cancelled()

        try:
            return fn()
        except TransientError:
            if retry_number >= attempts:
                raise

            backoff = base_delay * (2**retry_number)
            delay = backoff + random.uniform(0.0, backoff * 0.1)
            LOGGER.warning(
                "Transient failure; retry %d/%d in %.3f seconds",
                retry_number + 1,
                attempts,
                delay,
            )
            if wait_event.wait(delay) and cancel is not None:
                cancel.raise_if_cancelled()

    raise AssertionError("retry loop must return or raise")
