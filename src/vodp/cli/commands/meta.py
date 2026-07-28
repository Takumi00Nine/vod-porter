"""``vodp meta set`` (M1改: read current snippet -> merge -> update, never
drops title/description/tags/categoryId that the caller didn't touch).

M22 footer protection: whenever the description is actually touched
(``--description-file`` and/or ``--playlist-id``), the ``[vodp:vod=...]``
footer is re-derived from the *current* YouTube description and
re-applied -- ``meta set`` can never silently reassign a video's tracked
VOD identity to a different one (design.md §7).
"""

from __future__ import annotations

import re
from pathlib import Path

from vodp.core import exitcodes, jsonout, reasons
from vodp.core.description import build_description_for_meta_set, converge_playlist_line_only, playlist_url_for
from vodp.core.errors import ArgError, EnvironmentError_
from vodp.core.profile import load_profile, verify_expected_channel
from vodp.infra import auth as auth_infra
from vodp.infra import youtube as yt_infra

_FOOTER_LINE_RE = re.compile(r"^\[vodp:vod=(?P<vod_id>[^\]]+)\]$")


def _split_tags(tags_arg: str | None) -> list[str] | None:
    if tags_arg is None:
        return None
    return [t.strip() for t in tags_arg.split(",") if t.strip()]


def _read_description(path: str | None) -> str | None:
    if path is None:
        return None
    p = Path(path)
    if not p.is_file():
        raise EnvironmentError_(f"--description-file not found: {path}")
    return p.read_text(encoding="utf-8")


def _extract_footer_vod_id(description: str) -> str | None:
    lines = [line.rstrip() for line in description.replace("\r\n", "\n").split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    if not lines:
        return None
    m = _FOOTER_LINE_RE.match(lines[-1])
    return m.group("vod_id") if m else None


def run_set(args, ctx):
    if getattr(args, "privacy", None) is not None:
        # R9 (Codex/tester review round 2): `--privacy` is a shared flag
        # (parser.py's _add_meta_base, also used by `vods upload`), but
        # `meta set` only ever touches part=snippet -- a privacyStatus
        # change belongs to `videos publish`, which does the readback
        # verification (M11) that a privacy change actually needs. Rather
        # than silently accept and ignore it (previously exited 0 having
        # done nothing with the flag at all), refuse it outright so the
        # caller isn't misled into thinking it took effect.
        raise ArgError(
            "`meta set --privacy` is not supported -- use `vodp videos publish "
            "--video-id ... --privacy ...` instead (it also verifies the change via readback)"
        )
    description_file_content = _read_description(args.description_file)
    tags = _split_tags(args.tags)
    playlist_id = getattr(args, "playlist_id", None)
    playlist_url = playlist_url_for(playlist_id) if playlist_id else None

    if args.dry_run:
        envelope = jsonout.build_envelope(run=ctx.run_header(), videos=[{
            "twitch_vod_id": None, "status": "skipped", "reason_code": reasons.DRY_RUN,
            "youtube_video_id": args.video_id,
        }])
        return exitcodes.OK, envelope

    creds = auth_infra.authenticate(ctx.home, non_interactive=args.non_interactive)
    youtube = yt_infra.build_client(creds)
    # F5 (Codex/tester review round 2, differed-items follow-up,
    # requirements.md 推奨S7): no-op unless the profile sets
    # expected_channel_id.
    profile = load_profile(ctx.home, args.profile)
    if profile.expected_channel_id:
        channel_id = yt_infra.get_own_channel_id(youtube)
        verify_expected_channel(profile.expected_channel_id, channel_id)

    description = None
    current_snippet = None
    if description_file_content is not None or playlist_url is not None:
        current_snippet = yt_infra.get_video_snippet(youtube, args.video_id)
        current_description = current_snippet.get("description") or ""
        vod_id = _extract_footer_vod_id(current_description)
        base_text = description_file_content if description_file_content is not None else current_description
        if vod_id is None:
            # No prior vodp footer to protect/re-associate with -- still
            # converge the playlist line (a bare --playlist-id must not
            # be silently dropped just because there's no footer to
            # anchor to, Codex review major #6), just skip footer append.
            description = converge_playlist_line_only(base_text, playlist_url)
        else:
            description = build_description_for_meta_set(base_text, twitch_vod_id=vod_id, playlist_url=playlist_url)

    yt_infra.update_snippet(
        youtube, args.video_id, current_snippet=current_snippet,
        title=args.title, description=description, tags=tags, category_id=args.category_id,
    )
    envelope = jsonout.build_envelope(run=ctx.run_header(), videos=[{
        "twitch_vod_id": None, "status": "uploaded", "reason_code": None,
        "youtube_video_id": args.video_id,
    }])
    return exitcodes.OK, envelope
