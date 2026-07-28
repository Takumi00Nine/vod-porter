"""M8 JSON contract: a single JSON document on stdout per invocation.

Shape (see schemas/*.json for the per-command JSON Schema)::

    {
      "run": {"run_id": ..., "command": ..., "started_at": ..., "dry_run": ...},
      "videos": [{"twitch_vod_id": ..., "status": "uploaded|skipped|failed", "reason_code": ...}, ...],
      "summary": {"total": ..., "uploaded": ..., "skipped": ..., "failed": ...}
    }

Human-readable progress/errors must never go to stdout -- only here, and
only once per process (argparse-level exit 2 is the sole exception, see
core/exitcodes.py).
"""

from __future__ import annotations

import json
import sys
from typing import Any


def build_envelope(
    *,
    run: dict[str, Any],
    videos: list[dict[str, Any]] | None = None,
    summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    videos = videos if videos is not None else []
    if summary is None:
        summary = {
            "total": len(videos),
            "uploaded": sum(1 for v in videos if v.get("status") == "uploaded"),
            "skipped": sum(1 for v in videos if v.get("status") == "skipped"),
            "failed": sum(1 for v in videos if v.get("status") == "failed"),
        }
    return {"run": run, "videos": videos, "summary": summary}


def emit(envelope: dict[str, Any], stream=None) -> None:
    """Write the single JSON document for this invocation to stdout."""
    stream = stream if stream is not None else sys.stdout
    json.dump(envelope, stream, ensure_ascii=False, indent=2, sort_keys=False)
    stream.write("\n")
    stream.flush()
