"""M15 finite retry with exponential backoff -- idempotent calls only.

§4 is explicit that the three non-idempotent insert calls (videos.insert,
playlists.insert, playlistItems.insert) get num_retries=0 and never pass
through here (see infra/youtube.py: those functions don't even accept a
``retry_budget`` parameter); only idempotent GET/list/``videos.update``
(infra/twitch.py's ``_authed_get``, infra/youtube.py's read/update
helpers) use this budget, shared for the whole run via a single
``RetryBudget`` instance created once in ``cli/commands/vods.py``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

RETRYABLE_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})


def is_retryable_status(status_code: int | None) -> bool:
    return status_code in RETRYABLE_HTTP_STATUS


@dataclass
class RetryBudget:
    """``max_attempts`` is the max number of *retries* after the first
    call (i.e. max_attempts=5 allows up to 6 calls total: 1 + 5 retries).
    Kept as ``max_attempts`` for backwards compatibility with existing
    callers/tests; read it as "retry budget", not "total call count"."""

    max_attempts: int = 5
    base_delay: float = 1.0
    max_delay: float = 30.0
    attempts_used: int = 0

    def can_retry(self) -> bool:
        return self.attempts_used < self.max_attempts

    def next_delay(self) -> float:
        delay = min(self.max_delay, self.base_delay * (2**self.attempts_used))
        self.attempts_used += 1
        return delay


def call_with_retry(func, *, budget: RetryBudget, is_retryable, deadline=None, sleep=None):
    """Call ``func()``; on an exception where ``is_retryable(exc)`` is True,
    sleep with exponential backoff and retry while the budget/deadline allow.
    Re-raises once the budget is exhausted or the exception isn't retryable.

    ``sleep`` defaults to ``None`` and resolves to ``time.sleep`` at call
    time (not at function-definition time) so tests can monkeypatch the
    global ``time.sleep`` instead of needing to thread a fake through
    every call site (same reasoning as ``infra/youtube.py``'s
    ``verify_privacy``).
    """
    sleep_fn = sleep if sleep is not None else time.sleep
    while True:
        if deadline is not None:
            deadline.check()  # never start (another) attempt once the budget is gone
        try:
            return func()
        except Exception as exc:  # noqa: BLE001 - re-raised below when not retryable
            if not is_retryable(exc) or not budget.can_retry():
                raise
            if deadline is not None:
                deadline.check()
            delay = budget.next_delay()
            if deadline is not None:
                delay = min(delay, deadline.remaining())
            sleep_fn(delay)
