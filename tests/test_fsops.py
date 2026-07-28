"""Durable write primitives backing the ledger and auth token storage."""

from __future__ import annotations

import stat

from vodp.infra.fsops import append_line_fsync, atomic_write_bytes


def test_atomic_write_bytes_creates_file_with_mode(tmp_path):
    target = tmp_path / "sub" / "file.bin"
    atomic_write_bytes(target, b"hello", mode=0o600)
    assert target.read_bytes() == b"hello"
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600


def test_atomic_write_bytes_overwrites_existing(tmp_path):
    target = tmp_path / "file.bin"
    atomic_write_bytes(target, b"first")
    atomic_write_bytes(target, b"second")
    assert target.read_bytes() == b"second"


def test_atomic_write_leaves_no_tmp_files_behind(tmp_path):
    target = tmp_path / "file.bin"
    atomic_write_bytes(target, b"data")
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".file.bin.tmp-")]
    assert leftovers == []


def test_append_line_fsync_appends_multiple_lines(tmp_path):
    target = tmp_path / "log.jsonl"
    append_line_fsync(target, '{"a": 1}')
    append_line_fsync(target, '{"a": 2}')
    lines = target.read_text(encoding="utf-8").splitlines()
    assert lines == ['{"a": 1}', '{"a": 2}']
