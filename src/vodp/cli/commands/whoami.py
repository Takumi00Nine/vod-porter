"""``vodp whoami`` (M1: current authenticated YouTube channel identity)."""

from __future__ import annotations

from vodp.core import exitcodes, jsonout
from vodp.infra import auth as auth_infra
from vodp.infra import youtube as yt_infra


def run_whoami(args, ctx):
    creds = auth_infra.authenticate(ctx.home, non_interactive=args.non_interactive)
    youtube = yt_infra.build_client(creds)
    # F2 (Codex/tester review round 2, differed-items follow-up): routed
    # through infra/youtube.py's _execute() (via get_own_channel_snippet)
    # instead of a raw .execute() call, so a 401 here gets the same
    # AuthRequiredError(4) conversion every other call site gets, instead
    # of leaking an unconverted HttpError to main.py's generic exit-5
    # defensive branch.
    channel = yt_infra.get_own_channel_snippet(youtube)
    videos = [{
        "twitch_vod_id": None, "status": "skipped", "reason_code": None,
        "youtube_channel_id": channel["id"], "title": channel["snippet"]["title"],
    }]
    envelope = jsonout.build_envelope(run=ctx.run_header(), videos=videos)
    return exitcodes.OK, envelope
