"""vodp entry point: parse -> build RunContext -> dispatch -> single JSON out.

Every command handler returns a ``(exit_code, envelope)`` pair (see
core/jsonout.py for the envelope shape). Handlers raise VodpError subclasses
(core/errors.py) for the failure paths; main() converts those uniformly so
no command needs to touch sys.exit or print() directly.
"""

from __future__ import annotations

import os
import sys
import traceback

from vodp.cli.parser import parse_args
from vodp.core import exitcodes, jsonout
from vodp.core.errors import VodpError
from vodp.core.runctx import RunContext, resolve_home

_DISPATCH: dict[tuple[str, str | None], object] = {}


def _build_dispatch():
    if _DISPATCH:
        return _DISPATCH
    # Imported lazily so `vodp --help` doesn't need every optional dependency
    # (google-api-python-client, yt_dlp, ...) importable just to print usage.
    from vodp.cli.commands import (
        auth as cmd_auth,
        cache as cmd_cache,
        channel as cmd_channel,
        check as cmd_check,
        launchd as cmd_launchd,
        ledger as cmd_ledger,
        meta as cmd_meta,
        playlist as cmd_playlist,
        videos as cmd_videos,
        vods as cmd_vods,
        whoami as cmd_whoami,
    )

    _DISPATCH.update({
        ("vods", "list"): cmd_vods.run_list,
        ("vods", "upload"): cmd_vods.run_upload,
        ("videos", "list"): cmd_videos.run_list,
        ("videos", "status"): cmd_videos.run_status,
        ("videos", "publish"): cmd_videos.run_publish,
        ("videos", "delete"): cmd_videos.run_delete,
        ("meta", "set"): cmd_meta.run_set,
        ("playlist", "list"): cmd_playlist.run_list,
        ("playlist", "create"): cmd_playlist.run_create,
        ("playlist", "add"): cmd_playlist.run_add,
        ("playlist", "update"): cmd_playlist.run_update,
        ("playlist", "delete"): cmd_playlist.run_delete,
        ("playlist", "remove-item"): cmd_playlist.run_remove_item,
        ("channel", "show"): cmd_channel.run_show,
        ("channel", "update"): cmd_channel.run_update,
        ("cache", "clean"): cmd_cache.run_clean,
        ("ledger", "recover"): cmd_ledger.run_recover,
        ("ledger", "repair"): cmd_ledger.run_repair,
        ("ledger", "resolve"): cmd_ledger.run_resolve,
        ("auth", "login"): cmd_auth.run_login,
        ("auth", "migrate"): cmd_auth.run_migrate,
        ("launchd", "generate"): cmd_launchd.run_generate,
        ("check", None): cmd_check.run_check,
        ("whoami", None): cmd_whoami.run_whoami,
    })
    return _DISPATCH


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)  # argparse itself may sys.exit(2) here -- no JSON, by design (M4)

    home = resolve_home(getattr(args, "home", None), dict(os.environ))
    ctx = RunContext(
        home=home,
        command=f"{args.command} {args.subcommand}".strip() if args.subcommand else args.command,
        dry_run=bool(getattr(args, "dry_run", False)),
        non_interactive=bool(getattr(args, "non_interactive", False)),
    )

    dispatch = _build_dispatch()
    handler = dispatch.get((args.command, args.subcommand))
    if handler is None:  # pragma: no cover - unreachable if parser.py stays in sync
        print(f"unimplemented command: {args.command} {args.subcommand}", file=sys.stderr)
        return exitcodes.ARG_ERROR

    try:
        exit_code, envelope = handler(args, ctx)
    except VodpError as e:
        # M19 AC (requirements.md M19: sentinel secret must never appear
        # in stdout/stderr/the log): whichever handler already ran may
        # have registered known secrets via ctx.register_secrets() --
        # redact them here too, not just in the log file (Codex light
        # review, round-2 follow-up: R12 originally only wired secrets
        # into the logger, leaving this exact stderr/JSON path unmasked).
        message = ctx.redact(e.message)
        print(message, file=sys.stderr)
        if e.exit_code == exitcodes.ARG_ERROR:
            # M4/M8: exit 2 is the "bad CLI argument" code and never carries
            # a JSON envelope -- this holds regardless of whether argparse
            # itself caught it or a handler raised ArgError after parsing
            # (e.g. --range start>end, a preflight content check). Only
            # stderr text + exit 2, exactly like an argparse.error() would
            # have produced (Codex/tester review round 2, R1).
            return e.exit_code
        envelope = jsonout.build_envelope(
            run=ctx.run_header(),
            videos=[],
            summary={"total": 0, "uploaded": 0, "skipped": 0, "failed": 0, "error": message},
        )
        jsonout.emit(envelope)
        return e.exit_code
    except Exception as e:  # pragma: no cover - defensive: never crash without JSON+5
        tb_text = ctx.redact(traceback.format_exc())
        print(tb_text, file=sys.stderr, end="")
        message = ctx.redact(str(e))
        envelope = jsonout.build_envelope(
            run=ctx.run_header(),
            videos=[],
            summary={"total": 0, "uploaded": 0, "skipped": 0, "failed": 0, "error": message},
        )
        jsonout.emit(envelope)
        return exitcodes.ENV_OR_API_ERROR

    jsonout.emit(envelope)
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
