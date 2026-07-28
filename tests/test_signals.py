"""M15 AC: 1st signal flags only, 2nd raises GracefulAbort, 3rd os._exit(5)."""

from __future__ import annotations

import pytest

from vodp.infra.signals import GracefulAbort, SignalController


def test_first_signal_only_sets_flag():
    ctrl = SignalController()
    ctrl._handle(15, None)
    assert ctrl.interrupted is True
    assert ctrl.count == 1


def test_second_signal_raises_graceful_abort():
    ctrl = SignalController()
    ctrl._handle(15, None)
    with pytest.raises(GracefulAbort):
        ctrl._handle(15, None)


def test_third_signal_calls_os_exit(monkeypatch):
    calls = []
    monkeypatch.setattr("os._exit", lambda code: calls.append(code))
    ctrl = SignalController()
    ctrl._handle(15, None)
    with pytest.raises(GracefulAbort):
        ctrl._handle(15, None)
    ctrl._handle(15, None)
    assert calls == [5]


def test_install_and_restore_roundtrip():
    import signal

    original = signal.getsignal(signal.SIGTERM)
    with SignalController() as ctrl:
        installed = signal.getsignal(signal.SIGTERM)
        assert installed.__func__ is ctrl._handle.__func__
        assert installed.__self__ is ctrl
    assert signal.getsignal(signal.SIGTERM) is original
