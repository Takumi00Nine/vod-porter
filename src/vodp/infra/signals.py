"""M15 SIGTERM/SIGINT ladder: 1st=flag only, 2nd=abort in-flight I/O via
exception (caller's finally settles the ledger + emits the single JSON
document + exits 5), 3rd=os._exit(5) unconditionally.
"""

from __future__ import annotations

import os
import signal


class GracefulAbort(RuntimeError):
    """Raised on the 2nd signal to unwind out of in-flight I/O."""


class SignalController:
    def __init__(self):
        self.count = 0
        self._prev_handlers: dict[int, object] = {}

    def install(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            self._prev_handlers[sig] = signal.signal(sig, self._handle)

    def restore(self) -> None:
        for sig, handler in self._prev_handlers.items():
            signal.signal(sig, handler)
        self._prev_handlers.clear()

    def _handle(self, signum, frame):
        self.count += 1
        if self.count == 1:
            return
        if self.count == 2:
            raise GracefulAbort(f"signal {signum} received twice; aborting")
        os._exit(5)  # 3rd+: no more grace

    @property
    def interrupted(self) -> bool:
        return self.count >= 1

    def __enter__(self):
        self.install()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.restore()
        return False
