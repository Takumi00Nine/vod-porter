"""``vodp check`` (S4/S6: side-effect-free read-only diagnostic, M4/M8).

Never writes: no ledger, no lock, no token save, no log file, no mkdir of
downloads/. Reports what an operator needs before trusting an unattended
run: env config presence, profile loadability, ffprobe on PATH, and (only
if a usable token already exists) the authenticated channel identity.

M4 priority (2>4>5>6>3>0, Codex/tester review round 2, R3): each check's
own natural exit code (e.g. ``AuthRequiredError`` -> 4) is preserved and
combined via ``exitcodes.combine()`` -- a single flat "any check failed
-> 6" would silently downgrade a real auth-required (4) or environment
(5) condition into a generic partial-failure code.
"""

from __future__ import annotations

import shutil

from vodp.core import exitcodes, jsonout
from vodp.core.errors import ArgError, EnvironmentError_, VodpError
from vodp.core.profile import load_profile
from vodp.core.timerange import parse_range
from vodp.infra import auth as auth_infra
from vodp.infra import youtube as yt_infra
from vodp.infra.envconfig import load_env, require_twitch_config


def _check_ffprobe() -> str:
    path = shutil.which("ffprobe")
    if not path:
        raise EnvironmentError_("ffprobe not found on PATH")
    return path


def _check_one(name, fn):
    try:
        detail = fn()
        return {
            "twitch_vod_id": None, "status": "skipped", "reason_code": "ok",
            "check": name, "detail": detail, "_exit_code": exitcodes.OK,
        }
    except ArgError:
        # M4/M8 (Codex/tester review round 2, R3 follow-up): exit 2 is
        # the CLI-argument code and must go through main.py's exception
        # path (stderr text only, never a JSON envelope) exactly like
        # every other command -- catching it here into an ordinary
        # "failed" check result would let exitcodes.combine() surface
        # code 2 through the *normal* (non-exception) return path,
        # which main.py's ARG_ERROR-no-JSON guard only checks for on the
        # exception path. A malformed profile template (ArgError) must
        # still abort `check` the same way it aborts every other command.
        raise
    except VodpError as e:
        return {
            "twitch_vod_id": None, "status": "failed", "reason_code": e.reason_code or "check_failed",
            "check": name, "detail": e.message, "_exit_code": e.exit_code,
        }


def run_check(args, ctx):
    if args.range:
        parse_range(*args.range)  # validates only; check never fetches VODs itself

    results = []
    results.append(_check_one("twitch_env", lambda: require_twitch_config(load_env(ctx.home)) and "present"))
    results.append(_check_one("profile", lambda: load_profile(ctx.home, args.profile).name))
    results.append(_check_one("ffprobe", _check_ffprobe))

    def _youtube_auth():
        # M7/M19 (Codex/tester review round 2, R2 follow-up): `check`'s
        # own docstring promises "no token save" -- but without
        # dry_run=True, an expired-but-refreshable token would still get
        # a refreshed copy written to disk here (state C, infra/auth.py),
        # directly contradicting that promise.
        creds = auth_infra.authenticate(ctx.home, non_interactive=True, dry_run=True)
        youtube = yt_infra.build_client(creds)
        return yt_infra.get_own_channel_id(youtube)

    results.append(_check_one("youtube_auth", _youtube_auth))

    exit_code = exitcodes.combine(*(r["_exit_code"] for r in results))
    videos = [{k: v for k, v in r.items() if k != "_exit_code"} for r in results]
    envelope = jsonout.build_envelope(
        run=ctx.run_header(), videos=videos,
        summary={
            "total": len(videos),
            "uploaded": 0,
            "skipped": sum(1 for r in videos if r["status"] == "skipped"),
            "failed": sum(1 for r in videos if r["status"] == "failed"),
        },
    )
    return exit_code, envelope
