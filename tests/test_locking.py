"""M16 AC: a second concurrent invocation for the same lock must fail
(non-blocking exclusive flock), not hang."""

from __future__ import annotations

import os

import pytest

from vodp.infra.locking import LockAcquireError, exclusive_lock, shared_lock_if_exists


def test_exclusive_lock_creates_parent_dirs(tmp_path):
    lock_path = tmp_path / "run" / "channels" / "abc.lock"
    with exclusive_lock(lock_path):
        assert lock_path.exists()


def test_second_exclusive_lock_fails_with_separate_fd(tmp_path):
    lock_path = tmp_path / "channel.lock"
    with exclusive_lock(lock_path):
        # Simulate a second process by opening a brand new fd to the same file.
        import fcntl

        fd2 = os.open(str(lock_path), os.O_RDWR)
        try:
            with pytest.raises(OSError):
                fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd2)


def test_exclusive_lock_released_after_context_exit(tmp_path):
    lock_path = tmp_path / "channel.lock"
    with exclusive_lock(lock_path):
        pass
    with exclusive_lock(lock_path):
        pass  # must not raise LockAcquireError now that it's released


def test_lock_acquire_error_type(tmp_path):
    lock_path = tmp_path / "channel.lock"
    import fcntl

    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(LockAcquireError):
            with exclusive_lock(lock_path):
                pass
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_shared_lock_if_exists_noop_when_missing(tmp_path):
    lock_path = tmp_path / "nope.lock"
    with shared_lock_if_exists(lock_path) as fd:
        assert fd is None
    assert not lock_path.exists()  # dry-run must never create the lock file


def test_blocking_exclusive_lock_waits_instead_of_failing(tmp_path):
    import threading
    import time

    lock_path = tmp_path / "registry.lock"
    order = []

    def holder():
        with exclusive_lock(lock_path):
            order.append("holder-acquired")
            time.sleep(0.1)
            order.append("holder-released")

    t = threading.Thread(target=holder)
    t.start()
    time.sleep(0.02)  # let the holder acquire first
    with exclusive_lock(lock_path, blocking=True):
        order.append("waiter-acquired")
    t.join()
    assert order == ["holder-acquired", "holder-released", "waiter-acquired"]


def test_shared_lock_if_exists_joins_when_present(tmp_path):
    lock_path = tmp_path / "present.lock"
    lock_path.write_text("")
    with shared_lock_if_exists(lock_path) as fd:
        assert fd is not None


def test_shared_lock_if_exists_falls_back_when_exclusively_held(tmp_path):
    """Must never block: if the lock is exclusively held by another
    process (simulated via a second fd in this process), fall back to a
    lockless snapshot read instead of hanging."""
    lock_path = tmp_path / "held.lock"
    with exclusive_lock(lock_path):
        with shared_lock_if_exists(lock_path) as fd:
            assert fd is None  # fell back, did not block
