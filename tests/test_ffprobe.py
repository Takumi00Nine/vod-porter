"""M14/M18 AC: ffprobe wraps subprocess, non-zero/garbage output -> exit 5."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from vodp.core.errors import EnvironmentError_
from vodp.infra.ffprobe import probe_duration_seconds


@dataclass
class _FakeCompleted:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def test_probe_duration_parses_stdout(tmp_path):
    def fake_run(cmd, **kwargs):
        return _FakeCompleted(0, stdout="123.45\n")

    assert probe_duration_seconds("/usr/bin/ffprobe", tmp_path / "f.mp4", run=fake_run) == 123.45


def test_probe_duration_nonzero_exit_raises_environment_error(tmp_path):
    def fake_run(cmd, **kwargs):
        return _FakeCompleted(1, stderr="no such file")

    with pytest.raises(EnvironmentError_):
        probe_duration_seconds("/usr/bin/ffprobe", tmp_path / "f.mp4", run=fake_run)


def test_probe_duration_garbage_stdout_raises_environment_error(tmp_path):
    def fake_run(cmd, **kwargs):
        return _FakeCompleted(0, stdout="not-a-number\n")

    with pytest.raises(EnvironmentError_):
        probe_duration_seconds("/usr/bin/ffprobe", tmp_path / "f.mp4", run=fake_run)
