"""``vodp cache clean`` (S6 recommendation: orphan cleanup by ledger state
+ filename, under the channel lock; mtime is only a safety margin, not
the primary criterion).

A ``vodp-<twitch_vod_id>.<ext>`` file (M17 naming) is a deletion candidate
only if the ledger has no row for that vod_id at all, or the row is
already in one of ``DELETE_ELIGIBLE_STAGES`` (terminal -- the pipeline
should already have removed the file itself, so this is leftover cruft).
Everything else is protected regardless of age: this is an *allowlist* of
what's safe to delete, not a blocklist of what to protect, so an unknown
future stage fails closed (never deleted) instead of fail-open.

The whole scan-decide-delete sequence runs under the channel's exclusive
flock (when ``--channel`` is given) so a concurrent ``vods upload`` can
never have a file it just reserved deleted out from under it; a plain
dry-run only ever joins an *existing* lock (never creates one) per the
M7 dry-run boundary.
"""

from __future__ import annotations

import time
from contextlib import contextmanager

from vodp.core import exitcodes, jsonout, reasons
from vodp.core.deletion import resolve_execute
from vodp.infra.ledger import Ledger, resolve_ledger_path
from vodp.infra.locking import LockAcquireError, exclusive_lock, shared_lock_if_exists

DEFAULT_MAX_AGE_HOURS = 24
# Allowlist, not a blocklist: only rows confirmed terminal are ever
# eligible, so an unrecognized/future stage value fails closed (Codex
# review: an ACTIVE_STAGES blocklist fails *open* on an unknown stage).
DELETE_ELIGIBLE_STAGES = frozenset({"completed", "rejected", "deleted"})


def _vod_id_from_filename(name: str) -> str | None:
    """``vodp-<vod_id>.<ext>`` -- vod_id is everything up to the *first*
    dot, so a yt-dlp partial download like ``vodp-12345.mp4.part`` still
    resolves to vod_id ``12345``, not ``12345.mp4`` (Codex review: a
    rightmost-partition misparsed multi-suffix filenames)."""
    if not name.startswith("vodp-"):
        return None
    stem = name[len("vodp-"):]
    vod_id, _, _rest = stem.partition(".")
    return vod_id or None


@contextmanager
def _guard_lock(ctx, channel_id: str | None, *, execute: bool):
    if not channel_id:
        yield None
        return
    lock_path = ctx.run_dir / "channels" / f"{channel_id}.lock"
    if execute:
        with exclusive_lock(lock_path) as fd:
            yield fd
    else:
        # Dry-run: never create a lock file that doesn't already exist
        # (M7 boundary) -- just join it if a real run is holding it.
        with shared_lock_if_exists(lock_path) as fd:
            yield fd


def run_clean(args, ctx):
    execute = resolve_execute(args)
    max_age_seconds = getattr(args, "max_age_hours", DEFAULT_MAX_AGE_HOURS) * 3600
    channel_id = getattr(args, "channel_id", None)
    now = time.time()

    try:
        with _guard_lock(ctx, channel_id, execute=execute):
            ledger = None
            if channel_id:
                ledger_path = resolve_ledger_path(ctx.home, channel_id, override=args.ledger, register=False)
                ledger = Ledger(ledger_path, channel_id=channel_id)

            candidates = []
            if ctx.downloads_dir.is_dir():
                for path in sorted(ctx.downloads_dir.glob("vodp-*")):
                    if not path.is_file():
                        continue
                    vod_id = _vod_id_from_filename(path.name)
                    if vod_id is None:
                        continue
                    if ledger is not None:
                        current = ledger.get_current(vod_id)
                        if current is not None and current.get("stage") not in DELETE_ELIGIBLE_STAGES:
                            continue  # not confirmed terminal -- never a candidate, any age
                    age = now - path.stat().st_mtime
                    if age >= max_age_seconds:
                        candidates.append((path, vod_id))

            videos = []
            for path, vod_id in candidates:
                if execute:
                    path.unlink(missing_ok=True)
                    videos.append({
                        "twitch_vod_id": vod_id, "status": "uploaded", "reason_code": None, "path": str(path),
                    })
                else:
                    videos.append({
                        "twitch_vod_id": vod_id, "status": "skipped", "reason_code": reasons.DRY_RUN,
                        "path": str(path),
                    })
    except LockAcquireError:
        envelope = jsonout.build_envelope(
            run=ctx.run_header(), videos=[],
            summary={"total": 0, "uploaded": 0, "skipped": 0, "failed": 0,
                     "error": f"channel {channel_id} is locked by another run"},
        )
        return exitcodes.ENV_OR_API_ERROR, envelope

    envelope = jsonout.build_envelope(
        run=ctx.run_header(), videos=videos,
        summary={"total": len(videos), "uploaded": sum(1 for v in videos if v["status"] == "uploaded"),
                 "skipped": sum(1 for v in videos if v["status"] == "skipped"), "failed": 0},
    )
    return exitcodes.OK, envelope
