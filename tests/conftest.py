"""Shared pytest fixtures: fully offline (M20).

``block_network`` is autouse so no test can accidentally reach a real
socket; tests that fake HTTP/OAuth do so via the Fake* classes below /
in each test module, never via a live connection.
"""

from __future__ import annotations

import socket

import pytest


class _NetworkBlockedError(RuntimeError):
    pass


def _blocked_connect(self, *args, **kwargs):
    raise _NetworkBlockedError("network access is blocked in tests (see conftest.py)")


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", _blocked_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked_connect)


@pytest.fixture
def vodp_home(tmp_path):
    home = tmp_path / "vodp_home"
    home.mkdir()
    return home
