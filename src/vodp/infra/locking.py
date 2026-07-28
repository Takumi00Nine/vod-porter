"""fcntl-based locking (M16 multi-run guard, §4 channel-level ledger lock).

Two flavours are used across vodp:
  * an exclusive, non-blocking lock held for an entire run
    (``<home>/run/channels/<channel_id>.lock``) -- a second concurrent
    invocation for the same channel must fail fast (M16 AC).
  * a shared lock for read-only participants (dry-run "join if present").

fcntl.flock is left as the real syscall in tests (per design.md §10): two
file descriptors in the *same* process correctly fail to double-acquire an
exclusive lock, which is exactly what the M16 AC exercises.
"""

from __future__ import annotations

import errno
import fcntl
import os
from contextlib import contextmanager
from pathlib import Path

_LOCK_CONFLICT_ERRNOS = (errno.EACCES, errno.EAGAIN)


class LockAcquireError(RuntimeError):
    """Raised when a non-blocking flock could not be acquired because
    another holder already has it (EACCES/EAGAIN only -- any other OSError
    is a real environment problem and is left to propagate as-is)."""


def _open_lock_fd(lock_path: Path) -> int:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    return os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)


@contextmanager
def exclusive_lock(lock_path: Path, *, blocking: bool = False):
    """Acquire LOCK_EX, creating the lock file/parent dirs.

    Non-blocking by default: raises LockAcquireError if another holder
    already has it (M16: a second concurrent *run* for the same channel
    must exit non-zero instead of blocking). Pass ``blocking=True`` for
    short, fast critical sections (e.g. the registry file) where briefly
    waiting out another writer is preferable to failing the whole command.
    """
    fd = _open_lock_fd(lock_path)
    try:
        try:
            flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
            fcntl.flock(fd, flags)
        except OSError as e:
            os.close(fd)
            if e.errno in _LOCK_CONFLICT_ERRNOS:
                raise LockAcquireError(f"could not lock {lock_path}: {e}") from e
            raise
        try:
            yield fd
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


@contextmanager
def shared_lock_if_exists(lock_path: Path):
    """Join an existing lock file with LOCK_SH; yields None without
    creating the file if it does not exist (dry-run boundary, M7/§6:
    "既存には LOCK_SH 参加・無ければスナップショット読取＋警告").

    Non-blocking: if the file exists but is exclusively held by a live
    run (LOCK_SH|LOCK_NB fails), this falls back to the same "snapshot
    read + warning" behavior as the missing-file case instead of blocking
    for however long that run takes -- a dry-run must never hang waiting
    on another process (Codex review: cache clean's dry-run path hung the
    test suite when a real run held the lock)."""
    if not lock_path.exists():
        yield None
        return
    fd = os.open(str(lock_path), os.O_RDONLY)
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            acquired = True
        except OSError as e:
            if e.errno not in _LOCK_CONFLICT_ERRNOS:
                raise
        if acquired:
            try:
                yield fd
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        else:
            yield None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
