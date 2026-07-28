"""M3 AC: date-only expansion to full JST day + start>end -> exit 2."""

from __future__ import annotations

import pytest

from vodp.core.errors import ArgError
from vodp.core.timerange import parse_range


def test_date_only_expands_to_full_day():
    start, end = parse_range("2026-07-01", "2026-07-01")
    assert (start.hour, start.minute, start.second, start.microsecond) == (0, 0, 0, 0)
    assert (end.hour, end.minute, end.second, end.microsecond) == (23, 59, 59, 999999)
    assert start.tzinfo is not None and end.tzinfo is not None


def test_slash_date_supported_for_legacy_compat():
    start, end = parse_range("2026/07/01", "2026/07/02")
    assert start.day == 1 and end.day == 2


def test_full_datetime_is_preserved():
    start, end = parse_range("2026-07-01 09:30:00", "2026-07-01 10:00:00")
    assert (start.hour, start.minute, start.second) == (9, 30, 0)
    assert (end.hour, end.minute, end.second) == (10, 0, 0)


def test_start_after_end_raises_arg_error():
    with pytest.raises(ArgError):
        parse_range("2026-07-05", "2026-07-01")


def test_garbage_input_raises_arg_error():
    with pytest.raises(ArgError):
        parse_range("not-a-date", "2026-07-01")


def test_same_instant_is_allowed():
    start, end = parse_range("2026-07-01 00:00:00", "2026-07-01 00:00:00")
    assert start == end
