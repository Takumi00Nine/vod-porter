"""``vodp videos list|publish|delete`` (M1改, M10, M11)."""

from __future__ import annotations

from vodp.core import exitcodes, jsonout, reasons
from vodp.core.deletion import resolve_execute
from vodp.core.profile import load_profile, verify_expected_channel
from vodp.infra import auth as auth_infra
from vodp.infra import youtube as yt_infra


def _client(ctx, args, *, dry_run=False):
    creds = auth_infra.authenticate(ctx.home, non_interactive=args.non_interactive, dry_run=dry_run)
    return yt_infra.build_client(creds)


def _verify_channel_if_configured(ctx, args, youtube):
    """F5 (Codex/tester review round 2, differed-items follow-up,
    requirements.md 推奨S7): a no-op unless the profile actually sets
    expected_channel_id -- otherwise, refuse a write-type command
    against the wrong authenticated account before it does anything."""
    profile = load_profile(ctx.home, args.profile)
    if profile.expected_channel_id:
        channel_id = yt_infra.get_own_channel_id(youtube)
        verify_expected_channel(profile.expected_channel_id, channel_id)


def run_list(args, ctx):
    youtube = _client(ctx, args)
    items = yt_infra.list_my_uploaded_videos(youtube)
    videos = []
    for item in items:
        content_details = item.get("contentDetails", {})
        snippet = item.get("snippet", {})
        videos.append({
            "twitch_vod_id": None,
            "status": "skipped",
            "reason_code": None,
            "youtube_video_id": content_details.get("videoId"),
            "title": snippet.get("title"),
            "published_at": snippet.get("publishedAt"),
        })
    envelope = jsonout.build_envelope(
        run=ctx.run_header(), videos=videos,
        summary={"total": len(videos), "uploaded": 0, "skipped": len(videos), "failed": 0},
    )
    exit_code = exitcodes.OK if videos else exitcodes.NO_TARGETS
    return exit_code, envelope


def run_publish(args, ctx):
    if args.dry_run:
        envelope = jsonout.build_envelope(run=ctx.run_header(), videos=[{
            "twitch_vod_id": None, "status": "skipped", "reason_code": reasons.DRY_RUN,
            "youtube_video_id": args.video_id,
        }])
        return exitcodes.OK, envelope

    youtube = _client(ctx, args)
    _verify_channel_if_configured(ctx, args, youtube)
    yt_infra.publish_video(youtube, args.video_id, args.privacy)
    verified = yt_infra.verify_privacy(youtube, args.video_id, args.privacy)
    if not verified:
        videos = [{
            "twitch_vod_id": None, "status": "failed", "reason_code": reasons.PRIVACY_LOCK,
            "youtube_video_id": args.video_id,
        }]
        envelope = jsonout.build_envelope(run=ctx.run_header(), videos=videos)
        return exitcodes.PARTIAL_FAILURE, envelope  # M11 AC: private-still -> 6

    videos = [{
        "twitch_vod_id": None, "status": "uploaded", "reason_code": None,
        "youtube_video_id": args.video_id,
    }]
    envelope = jsonout.build_envelope(run=ctx.run_header(), videos=videos)
    return exitcodes.OK, envelope


def run_status(args, ctx):
    """``vodp videos status`` (read-only diagnostic, team-lead-assigned
    2026-08-08): never calls a write-type API -- only
    ``yt_infra.get_videos_status_and_processing`` (``videos.list``).
    A requested id YouTube doesn't return is reported per-item as
    ``status: failed``/``reason_code: not_found`` rather than aborting
    the whole batch, so one bad id among several good ones still shows
    the others."""
    youtube = _client(ctx, args)
    found = yt_infra.get_videos_status_and_processing(youtube, args.video_ids)
    videos = []
    for video_id in args.video_ids:
        item = found.get(video_id)
        if item is None:
            videos.append({
                "twitch_vod_id": None, "status": "failed", "reason_code": reasons.NOT_FOUND,
                "youtube_video_id": video_id,
            })
            continue
        status = item.get("status", {})
        processing = item.get("processingDetails", {})
        progress = processing.get("processingProgress")
        videos.append({
            "twitch_vod_id": None, "status": "skipped", "reason_code": None,
            "youtube_video_id": video_id,
            "upload_status": status.get("uploadStatus"),
            "failure_reason": status.get("failureReason"),
            "rejection_reason": status.get("rejectionReason"),
            "privacy_status": status.get("privacyStatus"),
            "processing_status": processing.get("processingStatus"),
            "processing_failure_reason": processing.get("processingFailureReason"),
            "processing_progress": {
                "parts_total": progress.get("partsTotal"),
                "parts_processed": progress.get("partsProcessed"),
                "time_left_ms": progress.get("timeLeftMs"),
            } if progress is not None else None,
        })
    failed = sum(1 for v in videos if v["status"] == "failed")
    summary = {"total": len(videos), "uploaded": 0, "skipped": len(videos) - failed, "failed": failed}
    envelope = jsonout.build_envelope(run=ctx.run_header(), videos=videos, summary=summary)
    # M11's run_publish sets the same precedent (private-still -> 6): a
    # per-item problem within an otherwise-successful read is
    # PARTIAL_FAILURE, not a hard error -- the other requested ids still
    # resolved fine and are still reported.
    exit_code = exitcodes.PARTIAL_FAILURE if failed else exitcodes.OK
    return exit_code, envelope


def run_delete(args, ctx):
    execute = resolve_execute(args)
    # M7 (Codex/tester review round 2, R2 follow-up): the dry-run preview
    # path must not save a refreshed token to disk either -- only the
    # real --yes execution is allowed the ordinary (non-dry-run) auth
    # write behavior.
    youtube = _client(ctx, args, dry_run=not execute)
    _verify_channel_if_configured(ctx, args, youtube)
    snippet = yt_infra.get_video_snippet(youtube, args.video_id)  # readback before any delete

    if not execute:
        envelope = jsonout.build_envelope(run=ctx.run_header(), videos=[{
            "twitch_vod_id": None, "status": "skipped", "reason_code": reasons.DRY_RUN,
            "youtube_video_id": args.video_id, "title": snippet.get("title"),
        }])
        return exitcodes.OK, envelope

    yt_infra.delete_video(youtube, args.video_id)
    envelope = jsonout.build_envelope(run=ctx.run_header(), videos=[{
        "twitch_vod_id": None, "status": "uploaded", "reason_code": None,
        "youtube_video_id": args.video_id, "title": snippet.get("title"),
    }])
    return exitcodes.OK, envelope
