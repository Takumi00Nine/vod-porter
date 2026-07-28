"""M15 AC: deadline exceeded stops the run (via monkeypatched time.monotonic,
per design.md §10 -- no real sleeping in tests)."""

from __future__ import annotations

import pytest

from vodp.infra.deadline import Deadline, DeadlineExceededError


def test_remaining_counts_down(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr("time.monotonic", lambda: clock["t"])
    d = Deadline(total_seconds=10.0)
    assert d.remaining() == 10.0
    clock["t"] = 4.0
    assert d.remaining() == 6.0


def test_expired_true_past_deadline(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr("time.monotonic", lambda: clock["t"])
    d = Deadline(total_seconds=5.0)
    clock["t"] = 5.1
    assert d.expired() is True


def test_check_raises_when_expired(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr("time.monotonic", lambda: clock["t"])
    d = Deadline(total_seconds=1.0)
    clock["t"] = 2.0
    with pytest.raises(DeadlineExceededError):
        d.check()


def test_timeout_for_never_exceeds_remaining(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr("time.monotonic", lambda: clock["t"])
    d = Deadline(total_seconds=10.0)
    clock["t"] = 8.0
    assert d.timeout_for(cap=30.0) == 2.0
    assert d.timeout_for(cap=1.0) == 1.0
