"""ffprobe wrapper (M14/M18): post-download duration measurement.

Twitch's self-reported ``duration`` string lets us skip a too-long VOD
*before* downloading (M14 AC); ffprobe re-measures the actual file after
the download completes, because a truncated/transcoded file can disagree
with what Twitch reported.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from vodp.core.errors import EnvironmentError_


def probe_duration_seconds(ffprobe_path: str, file_path: Path, *, timeout: float = 30.0, run=subprocess.run) -> float:
    result = run(
        [ffprobe_path, "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", str(file_path)],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise EnvironmentError_(f"ffprobe failed for {file_path}: {result.stderr.strip()}")
    try:
        return float(result.stdout.strip())
    except ValueError as e:
        raise EnvironmentError_(f"ffprobe returned a non-numeric duration for {file_path}: {result.stdout!r}") from e
