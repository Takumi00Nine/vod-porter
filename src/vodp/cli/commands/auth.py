"""``vodp auth login|migrate`` (M6, S3 token pickle -> JSON migration)."""

from __future__ import annotations

from pathlib import Path

from vodp.core import exitcodes, jsonout
from vodp.core.errors import ArgError
from vodp.infra import auth as auth_infra


def run_login(args, ctx):
    if args.non_interactive:
        raise ArgError("`vodp auth login` requires an interactive terminal; omit --non-interactive")
    creds = auth_infra.authenticate(ctx.home, non_interactive=False)
    envelope = jsonout.build_envelope(run=ctx.run_header(), videos=[{
        "twitch_vod_id": None, "status": "uploaded", "reason_code": None,
        "token_path": str(auth_infra.token_path(ctx.home)),
        "scopes": list(creds.scopes or []),
    }])
    return exitcodes.OK, envelope


def run_migrate(args, ctx):
    pickle_path = args.pickle_path or str(ctx.home / "pickle" / "token.pickle")
    if not Path(pickle_path).exists():
        raise ArgError(f"--pickle-path not found: {pickle_path}")
    result = auth_infra.migrate_pickle(ctx.home, pickle_path)
    envelope = jsonout.build_envelope(run=ctx.run_header(), videos=[{
        "twitch_vod_id": None, "status": "uploaded", "reason_code": None,
        **result,
    }])
    return exitcodes.OK, envelope
