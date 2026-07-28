"""``vodp playlist list|create|add|delete|remove-item`` (M12, M1改)."""

from __future__ import annotations

from vodp.core import exitcodes, jsonout, reasons
from vodp.core.deletion import resolve_execute
from vodp.core.errors import ApiError, ArgError
from vodp.core.profile import load_profile, verify_expected_channel
from vodp.infra import auth as auth_infra
from vodp.infra import youtube as yt_infra


def _client(ctx, args, *, dry_run=False):
    creds = auth_infra.authenticate(ctx.home, non_interactive=args.non_interactive, dry_run=dry_run)
    return yt_infra.build_client(creds)


def _verify_channel_if_configured(ctx, args, youtube):
    """F5 (Codex/tester review round 2, differed-items follow-up,
    requirements.md 推奨S7): see videos.py's identical helper."""
    profile = load_profile(ctx.home, args.profile)
    if profile.expected_channel_id:
        channel_id = yt_infra.get_own_channel_id(youtube)
        verify_expected_channel(profile.expected_channel_id, channel_id)


def run_list(args, ctx):
    youtube = _client(ctx, args)
    items = yt_infra.list_my_playlists(youtube)
    videos = [
        {
            "twitch_vod_id": None, "status": "skipped", "reason_code": None,
            "playlist_id": p["id"], "title": p["snippet"]["title"],
        }
        for p in items
    ]
    envelope = jsonout.build_envelope(
        run=ctx.run_header(), videos=videos,
        summary={"total": len(videos), "uploaded": 0, "skipped": len(videos), "failed": 0},
    )
    exit_code = exitcodes.OK if videos else exitcodes.NO_TARGETS
    return exit_code, envelope


def run_create(args, ctx):
    if args.dry_run:
        envelope = jsonout.build_envelope(run=ctx.run_header(), videos=[{
            "twitch_vod_id": None, "status": "skipped", "reason_code": reasons.DRY_RUN,
        }])
        return exitcodes.OK, envelope

    youtube = _client(ctx, args)
    _verify_channel_if_configured(ctx, args, youtube)
    existing = yt_infra.find_playlist_by_name(youtube, args.name)
    if existing is not None:
        raise ArgError(f"a playlist named {args.name!r} already exists ({existing['id']})")
    created = yt_infra.create_playlist(youtube, args.name, args.privacy)
    envelope = jsonout.build_envelope(run=ctx.run_header(), videos=[{
        "twitch_vod_id": None, "status": "uploaded", "reason_code": None,
        "playlist_id": created["id"], "title": args.name,
    }])
    return exitcodes.OK, envelope


def run_add(args, ctx):
    if args.dry_run:
        envelope = jsonout.build_envelope(run=ctx.run_header(), videos=[{
            "twitch_vod_id": None, "status": "skipped", "reason_code": reasons.DRY_RUN,
            "youtube_video_id": args.video_id,
        }])
        return exitcodes.OK, envelope

    youtube = _client(ctx, args)
    _verify_channel_if_configured(ctx, args, youtube)
    playlist = yt_infra.find_playlist_by_name(youtube, args.name)
    if playlist is None:
        playlist = yt_infra.create_playlist(youtube, args.name, "public")
    playlist_id = playlist["id"]

    if yt_infra.playlist_contains_video(youtube, playlist_id, args.video_id):
        envelope = jsonout.build_envelope(run=ctx.run_header(), videos=[{
            "twitch_vod_id": None, "status": "skipped", "reason_code": reasons.ALREADY_UPLOADED,
            "youtube_video_id": args.video_id, "playlist_id": playlist_id,
        }])
        return exitcodes.OK, envelope

    yt_infra.add_video_to_playlist(youtube, playlist_id, args.video_id)
    envelope = jsonout.build_envelope(run=ctx.run_header(), videos=[{
        "twitch_vod_id": None, "status": "uploaded", "reason_code": None,
        "youtube_video_id": args.video_id, "playlist_id": playlist_id,
    }])
    return exitcodes.OK, envelope


def run_delete(args, ctx):
    execute = resolve_execute(args)
    # M7 (Codex/tester review round 2, R2 follow-up): dry-run preview
    # must not save a refreshed token to disk either.
    youtube = _client(ctx, args, dry_run=not execute)
    _verify_channel_if_configured(ctx, args, youtube)
    playlists = {p["id"]: p["snippet"]["title"] for p in yt_infra.list_my_playlists(youtube)}
    title = playlists.get(args.playlist_id)
    if title is None:
        # Misdeletion guard (design.md §3 "削除系": readback失敗なら削除
        # しない): the readback succeeded as an API call but did not
        # confirm this playlist_id actually exists -- refusing here
        # applies to *both* dry-run and --yes, since a preview that can't
        # even show a real title is not a trustworthy preview either
        # (Codex/tester review round 2, R5).
        raise ApiError(
            f"playlist {args.playlist_id!r} not found in playlists.list (mine=True) readback; refusing to delete"
        )

    if not execute:
        envelope = jsonout.build_envelope(run=ctx.run_header(), videos=[{
            "twitch_vod_id": None, "status": "skipped", "reason_code": reasons.DRY_RUN,
            "playlist_id": args.playlist_id, "title": title,
        }])
        return exitcodes.OK, envelope

    yt_infra.delete_playlist(youtube, args.playlist_id)
    envelope = jsonout.build_envelope(run=ctx.run_header(), videos=[{
        "twitch_vod_id": None, "status": "uploaded", "reason_code": None,
        "playlist_id": args.playlist_id, "title": title,
    }])
    return exitcodes.OK, envelope


def run_remove_item(args, ctx):
    execute = resolve_execute(args)
    youtube = _client(ctx, args, dry_run=not execute)
    _verify_channel_if_configured(ctx, args, youtube)
    items = yt_infra.list_playlist_items(youtube, args.playlist_id, part="id,snippet,contentDetails")
    match = next((it for it in items if it["contentDetails"]["videoId"] == args.video_id), None)

    if match is None:
        envelope = jsonout.build_envelope(run=ctx.run_header(), videos=[{
            "twitch_vod_id": None, "status": "skipped", "reason_code": reasons.ALREADY_UPLOADED,
            "youtube_video_id": args.video_id, "playlist_id": args.playlist_id,
        }])
        return exitcodes.OK, envelope

    if not execute:
        envelope = jsonout.build_envelope(run=ctx.run_header(), videos=[{
            "twitch_vod_id": None, "status": "skipped", "reason_code": reasons.DRY_RUN,
            "youtube_video_id": args.video_id, "playlist_id": args.playlist_id,
            "title": match["snippet"].get("title"),
        }])
        return exitcodes.OK, envelope

    yt_infra.remove_playlist_item(youtube, match["id"])
    envelope = jsonout.build_envelope(run=ctx.run_header(), videos=[{
        "twitch_vod_id": None, "status": "uploaded", "reason_code": None,
        "youtube_video_id": args.video_id, "playlist_id": args.playlist_id,
        "title": match["snippet"].get("title"),
    }])
    return exitcodes.OK, envelope
