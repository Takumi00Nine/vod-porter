"""Durable filesystem primitives shared by ledger + auth token storage.

Every write here follows the same "won't half-write under a crash" shape:
write to a private tmp file, fsync the data, atomically rename into place,
then fsync the parent directory so the rename itself is durable too.

Hardening notes (Codex review round 1): ``os.write`` is not guaranteed to
write every byte in one call, so ``atomic_write_bytes`` now loops until the
whole buffer is flushed; a failed write/fsync/replace now cleans up its own
tmp file instead of leaving it behind; ``append_line_fsync`` repairs a
missing trailing newline on an existing file *before* appending, so a
prior crash that left a complete-but-unterminated last line can never fuse
with the next append into one malformed ``}{`` line.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:  # pragma: no cover - defensive; os.write raises on real errors
            raise OSError(f"os.write returned {written} for a {len(view)}-byte remaining buffer")
        view = view[written:]


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """O_EXCL tmp -> fchmod -> write(all bytes) -> fsync -> os.replace -> dir fsync."""
    path = Path(path)
    durable_mkdir(path.parent)
    tmp_path = path.parent / f".{path.name}.tmp-{uuid.uuid4().hex}"
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        try:
            os.fchmod(fd, mode)
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


def _fsync_dir(dir_path: Path) -> None:
    """Durability is the entire point of this module, so a directory
    fsync failure is propagated, not swallowed (Codex review round 2:
    silently ignoring it defeated the "marker survives a crash" guarantee
    ledger.py relies on)."""
    dir_fd = os.open(str(dir_path), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def durable_mkdir(path: Path) -> None:
    """mkdir(parents=True) then fsync ``path`` and its immediate parent,
    so a newly-created directory entry survives a crash right after this
    call returns (Codex review round 2, #4 -- covers the common
    single-new-level case; it does not walk/fsync every ancestor level
    that may have been freshly created by ``parents=True``)."""
    path = Path(path)
    already_existed = path.is_dir()
    path.mkdir(parents=True, exist_ok=True)
    if not already_existed:
        _fsync_dir(path)
        _fsync_dir(path.parent)


def _ensure_trailing_newline(path: Path) -> None:
    """If ``path`` exists, is non-empty, and doesn't end with "\\n", append
    one. Guards against a torn previous write fusing with the next append."""
    size = path.stat().st_size
    if size == 0:
        return
    with open(path, "rb") as f:
        f.seek(-1, os.SEEK_END)
        last_byte = f.read(1)
    if last_byte != b"\n":
        with open(path, "ab") as f:
            f.write(b"\n")
            f.flush()
            os.fsync(f.fileno())


def append_line_fsync(path: Path, line: str) -> None:
    """Append ``line`` + "\\n" to ``path``, fsync'ing the write (§4 ledger)."""
    path = Path(path)
    is_new_file = not path.exists()
    durable_mkdir(path.parent)
    if not is_new_file:
        _ensure_trailing_newline(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line if line.endswith("\n") else line + "\n")
        f.flush()
        os.fsync(f.fileno())
    if is_new_file:
        _fsync_dir(path.parent)
