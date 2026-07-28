"""M15 unattended-run deadlines: connect/read/total, propagated to every
blocking call (requests timeout, googleapiclient transport, ffprobe
subprocess, yt-dlp progress_hook, OAuth wait).

Uses time.monotonic so tests can monkeypatch it (design.md §10) instead of
sleeping for real.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


class DeadlineExceededError(RuntimeError):
    pass


@dataclass
class Deadline:
    total_seconds: float
    _start: float = None  # type: ignore[assignment]

    def __post_init__(self):
        if self._start is None:
            self._start = time.monotonic()

    def remaining(self) -> float:
        elapsed = time.monotonic() - self._start
        return max(0.0, self.total_seconds - elapsed)

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def check(self) -> None:
        if self.expired():
            raise DeadlineExceededError(
                f"deadline of {self.total_seconds}s exceeded"
            )

    def timeout_for(self, cap: float | None = None) -> float:
        """A per-call timeout that never exceeds the remaining budget.

        Raises DeadlineExceededError instead of returning 0.0 when already
        expired -- a 0-second timeout means "no timeout" to some HTTP
        clients, which would defeat the whole point (Codex review #17).
        """
        self.check()
        remaining = self.remaining()
        if cap is None:
            return remaining
        return min(cap, remaining)
