"""M18 preflight checks, run before DL/insert and again on resume.

Split per the requirement's own boundary: content problems the user can fix
by changing arguments/profile are ArgError (exit 2); missing tools/files/
disk space are EnvironmentError_ (exit 5).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from vodp.core.errors import ArgError, EnvironmentError_

TITLE_MAX_CHARS = 100
DESCRIPTION_MAX_BYTES = 5000
TAGS_MAX_CHARS = 500
FORBIDDEN_CHARS = ("<", ">")
ESTIMATED_BITRATE_BYTES_PER_SEC = 0.75 * 1024 * 1024  # 0.75 MB/s
DISK_SPACE_SAFETY_FACTOR = 1.2


@dataclass
class PreflightInput:
    title: str
    description: str
    tags: list[str]
    description_file: str | None = None
    duration_seconds: float | None = None
    download_dir: Path | None = None
    ffprobe_path: str | None = None  # None -> use shutil.which


def _check_forbidden_chars(label: str, text: str) -> None:
    for ch in FORBIDDEN_CHARS:
        if ch in text:
            raise ArgError(f"{label} must not contain {ch!r}: {text!r}")


def check_title(title: str) -> None:
    if len(title) > TITLE_MAX_CHARS:
        raise ArgError(f"title exceeds {TITLE_MAX_CHARS} characters ({len(title)})")
    _check_forbidden_chars("title", title)


def check_description(description: str) -> None:
    size = len(description.encode("utf-8"))
    if size > DESCRIPTION_MAX_BYTES:
        raise ArgError(f"description exceeds {DESCRIPTION_MAX_BYTES} bytes (UTF-8, got {size})")
    _check_forbidden_chars("description", description)


def check_tags(tags: list[str]) -> None:
    total = sum(len(t) for t in tags)
    if total > TAGS_MAX_CHARS:
        raise ArgError(f"tags exceed {TAGS_MAX_CHARS} total characters ({total})")
    for t in tags:
        _check_forbidden_chars("tag", t)


def check_description_file(path: str | None) -> None:
    if path is None:
        return
    if not Path(path).is_file():
        raise EnvironmentError_(f"--description-file not found: {path}")


def check_ffprobe(ffprobe_path: str | None) -> str:
    if ffprobe_path:
        if not Path(ffprobe_path).is_file():
            raise EnvironmentError_(f"ffprobe not found: {ffprobe_path}")
        return ffprobe_path
    resolved = shutil.which("ffprobe")
    if not resolved:
        raise EnvironmentError_("ffprobe not found on PATH")
    return resolved


def required_disk_bytes(duration_seconds: float) -> int:
    return int(duration_seconds * ESTIMATED_BITRATE_BYTES_PER_SEC * DISK_SPACE_SAFETY_FACTOR)


def check_disk_space(download_dir: Path, duration_seconds: float) -> None:
    """Check free space without creating ``download_dir`` (M7: no FS writes
    during preflight -- the downloader creates the directory later)."""
    required = required_disk_bytes(duration_seconds)
    probe_dir = download_dir if download_dir.is_dir() else download_dir.parent
    usage = shutil.disk_usage(probe_dir)
    if usage.free < required:
        raise EnvironmentError_(
            f"insufficient disk space in {download_dir}: need {required} bytes, have {usage.free}"
        )


def run_preflight(data: PreflightInput) -> None:
    """Run all applicable checks; raises ArgError or EnvironmentError_."""
    check_title(data.title)
    check_description(data.description)
    check_tags(data.tags)
    check_description_file(data.description_file)
    if data.duration_seconds is not None and data.download_dir is not None:
        check_disk_space(data.download_dir, data.duration_seconds)
    check_ffprobe(data.ffprobe_path)
