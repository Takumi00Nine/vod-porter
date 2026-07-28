"""--range parsing shared by ``vods list``/``vods upload`` and ``check`` (M3).

Date-only inputs expand to the full JST day: 00:00:00.000000 for the start,
23:59:59.999999 for the end. ``start > end`` is an argument error (exit 2).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from vodp.core.errors import ArgError

JST = timezone(timedelta(hours=9))

_DATE_RE = re.compile(r"^(?P<y>\d{4})[-/](?P<m>\d{2})[-/](?P<d>\d{2})$")
_DATETIME_RE = re.compile(
    r"^(?P<y>\d{4})[-/](?P<m>\d{2})[-/](?P<d>\d{2})[ T]"
    r"(?P<H>\d{2}):(?P<M>\d{2}):(?P<S>\d{2})(?:\.(?P<f>\d{1,6}))?$"
)


def _parse_one(value: str, *, is_end: bool) -> datetime:
    v = value.strip()
    m = _DATE_RE.fullmatch(v)
    if m:
        y, mo, d = int(m["y"]), int(m["m"]), int(m["d"])
        try:
            if is_end:
                return datetime(y, mo, d, 23, 59, 59, 999999, tzinfo=JST)
            return datetime(y, mo, d, 0, 0, 0, 0, tzinfo=JST)
        except ValueError as e:
            raise ArgError(f"invalid --range value: {value!r} ({e})") from e

    m = _DATETIME_RE.fullmatch(v)
    if m:
        frac = (m["f"] or "0").ljust(6, "0")[:6]
        try:
            return datetime(
                int(m["y"]), int(m["m"]), int(m["d"]),
                int(m["H"]), int(m["M"]), int(m["S"]), int(frac),
                tzinfo=JST,
            )
        except ValueError as e:
            raise ArgError(f"invalid --range value: {value!r} ({e})") from e

    raise ArgError(
        f"invalid --range value: {value!r} "
        "(expected YYYY-MM-DD or YYYY-MM-DD HH:MM:SS[.ffffff])"
    )


def parse_range(start: str, end: str) -> tuple[datetime, datetime]:
    """Return (start, end) as tz-aware JST datetimes; raises ArgError."""
    start_dt = _parse_one(start, is_end=False)
    end_dt = _parse_one(end, is_end=True)
    if start_dt > end_dt:
        raise ArgError(f"--range start must be <= end: {start!r} > {end!r}")
    return start_dt, end_dt
