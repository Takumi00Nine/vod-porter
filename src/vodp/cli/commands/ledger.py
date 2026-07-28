"""``vodp ledger recover|repair|resolve`` (M5 §4 admin surface).

These are the only commands allowed to touch a ledger while it might be
blocked (``repair``) or while resolving a stuck ``recovery_required`` row
(``resolve``); ``vods upload`` never does either automatically (exactly-once
over auto-recovery, §4).
"""

from __future__ import annotations

from vodp.core import exitcodes, jsonout
from vodp.core.errors import ArgError, LedgerError
from vodp.infra.ledger import Ledger, resolve_ledger_path
from vodp.infra.locking import LockAcquireError, exclusive_lock


def _ledger_for(ctx, channel_id: str, override: str | None) -> Ledger:
    path = resolve_ledger_path(ctx.home, channel_id, override=override)
    return Ledger(path, channel_id=channel_id)


def run_recover(args, ctx):
    channel_lock = ctx.run_dir / "channels" / f"{args.channel_id}.lock"
    try:
        with exclusive_lock(channel_lock):
            ledger = _ledger_for(ctx, args.channel_id, getattr(args, "ledger", None))
            entries = ledger.resumable_entries()
    except LockAcquireError as e:
        raise LedgerError(f"another vodp run holds the channel lock: {e}") from e
    videos = [
        {
            "twitch_vod_id": e["twitch_vod_id"],
            "status": "skipped",
            "reason_code": e.get("stage"),
            "youtube_video_id": e.get("youtube_video_id"),
        }
        for e in entries
    ]
    envelope = jsonout.build_envelope(run=ctx.run_header(), videos=videos)
    return exitcodes.OK, envelope


def run_repair(args, ctx):
    channel_lock = ctx.run_dir / "channels" / f"{args.channel_id}.lock"
    try:
        with exclusive_lock(channel_lock):
            ledger = _ledger_for(ctx, args.channel_id, getattr(args, "ledger", None))
            result = ledger.repair()
    except LockAcquireError as e:
        raise LedgerError(str(e)) from e
    envelope = jsonout.build_envelope(
        run=ctx.run_header(), videos=[],
        summary={"total": 0, "uploaded": 0, "skipped": 0, "failed": 0, **result},
    )
    exit_code = exitcodes.ENV_OR_API_ERROR if result.get("blocked") else exitcodes.OK
    return exit_code, envelope


def run_resolve(args, ctx):
    if not args.yes:  # argparse already enforces required=True, kept for defense in depth
        raise ArgError("--yes is required to resolve a ledger row")
    channel_lock = ctx.run_dir / "channels" / f"{args.channel_id}.lock"
    try:
        with exclusive_lock(channel_lock):
            ledger = _ledger_for(ctx, args.channel_id, getattr(args, "ledger", None))
            record = ledger.resolve(twitch_vod_id=args.vod_id, action=args.action)
    except LockAcquireError as e:
        raise LedgerError(f"another vodp run holds the channel lock: {e}") from e
    videos = [{
        "twitch_vod_id": args.vod_id,
        "status": "skipped" if args.action == "reinsert" else "failed",
        "reason_code": record.get("stage"),
        "youtube_video_id": record.get("youtube_video_id"),
    }]
    envelope = jsonout.build_envelope(run=ctx.run_header(), videos=videos)
    return exitcodes.OK, envelope
