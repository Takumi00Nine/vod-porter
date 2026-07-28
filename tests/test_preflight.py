"""M18 AC: each boundary raises the right exception class (2 vs 5)."""

from __future__ import annotations

import pytest

from vodp.core.errors import ArgError, EnvironmentError_
from vodp.core.preflight import (
    PreflightInput,
    check_description,
    check_ffprobe,
    check_tags,
    check_title,
    required_disk_bytes,
    run_preflight,
)


def test_title_at_100_chars_is_ok():
    check_title("a" * 100)


def test_title_over_100_chars_raises_arg_error():
    with pytest.raises(ArgError):
        check_title("a" * 101)


def test_title_with_angle_bracket_raises_arg_error():
    with pytest.raises(ArgError):
        check_title("a<b")


def test_description_at_5000_bytes_is_ok():
    check_description("a" * 5000)


def test_description_over_5000_bytes_raises_arg_error():
    with pytest.raises(ArgError):
        check_description("a" * 5001)


def test_description_byte_boundary_uses_utf8_not_chars():
    # each "あ" is 3 bytes in UTF-8: 1666*3=4998 bytes (ok), 1667*3=5001 (fail)
    check_description("あ" * 1666)
    with pytest.raises(ArgError):
        check_description("あ" * 1667)


def test_tags_over_500_chars_raises_arg_error():
    with pytest.raises(ArgError):
        check_tags(["a" * 250, "b" * 251])


def test_tags_at_500_chars_is_ok():
    check_tags(["a" * 250, "b" * 250])


def test_ffprobe_missing_raises_environment_error():
    with pytest.raises(EnvironmentError_):
        check_ffprobe("/no/such/ffprobe-binary")


def test_ffprobe_present_returns_path(tmp_path):
    fake = tmp_path / "ffprobe"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    assert check_ffprobe(str(fake)) == str(fake)


def test_description_file_missing_raises_environment_error(tmp_path):
    data = PreflightInput(
        title="t", description="d", tags=[],
        description_file=str(tmp_path / "nope.txt"),
        ffprobe_path=str(tmp_path / "ffprobe"),
    )
    (tmp_path / "ffprobe").write_text("#!/bin/sh\n")
    with pytest.raises(EnvironmentError_):
        run_preflight(data)


def test_run_preflight_does_not_create_download_dir(tmp_path):
    """M7 boundary: preflight must never mkdir (that's the downloader's job)."""
    download_dir = tmp_path / "downloads"
    ffprobe = tmp_path / "ffprobe"
    ffprobe.write_text("#!/bin/sh\n")
    data = PreflightInput(
        title="t", description="d", tags=[], duration_seconds=10.0,
        download_dir=download_dir, ffprobe_path=str(ffprobe),
    )
    run_preflight(data)
    assert not download_dir.exists()


def test_required_disk_bytes_matches_formula():
    assert required_disk_bytes(100) == int(100 * 0.75 * 1024 * 1024 * 1.2)
