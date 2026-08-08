"""``vodp channel show|update``: a read-only channel detail view (a
superset of whoami's id+title -- see whoami.py, which is unchanged and
stays the cheap identity check) plus a channel description merge-update.

``channel update`` only ever writes ``part=brandingSettings`` --
``channels.update`` does not support ``part=snippet`` at all (title/
description under ``snippet`` are read-only to API consumers; confirmed
against https://developers.google.com/youtube/v3/docs/channels/update).
It currently only exposes ``--description``: the API itself rejects any
*change* to ``brandingSettings.channel.title``
(``channelTitleUpdateForbidden`` -- renaming a channel is Studio-UI-only,
not available through this API at all), so there is no working
title-change flag to expose here."""

from __future__ import annotations

from vodp.core import exitcodes, jsonout, reasons
from vodp.core.profile import load_profile, verify_expected_channel
from vodp.infra import auth as auth_infra
from vodp.infra import youtube as yt_infra


def _client(ctx, args, *, dry_run=False):
    creds = auth_infra.authenticate(ctx.home, non_interactive=args.non_interactive, dry_run=dry_run)
    return yt_infra.build_client(creds)


def _verify_expected_channel_id(ctx, args, channel_id):
    """Same F5 guard as every other write-type command (playlist.py/
    videos.py/meta.py's ``_verify_channel_if_configured``) -- a no-op
    unless the profile sets expected_channel_id. Takes an already-fetched
    ``channel_id`` instead of the ``youtube`` client + its own
    ``get_own_channel_id()`` call like the other commands' helper does:
    ``channel update``'s own write target *is* the channel id (needed
    for the ``channels.update`` body regardless of this guard), so
    fetching it a second time here would be a redundant API call."""
    profile = load_profile(ctx.home, args.profile)
    if profile.expected_channel_id:
        verify_expected_channel(profile.expected_channel_id, channel_id)


def run_show(args, ctx):
    youtube = _client(ctx, args)
    channel = yt_infra.get_own_channel_detail(youtube)
    snippet = channel.get("snippet", {})
    envelope = jsonout.build_envelope(run=ctx.run_header(), videos=[{
        "twitch_vod_id": None, "status": "skipped", "reason_code": None,
        "youtube_channel_id": channel["id"], "title": snippet.get("title"),
        "description": snippet.get("description"), "custom_url": snippet.get("customUrl"),
    }])
    return exitcodes.OK, envelope


def run_update(args, ctx):
    if args.dry_run:
        envelope = jsonout.build_envelope(run=ctx.run_header(), videos=[{
            "twitch_vod_id": None, "status": "skipped", "reason_code": reasons.DRY_RUN,
            "description": args.description,
        }])
        return exitcodes.OK, envelope

    youtube = _client(ctx, args)
    channel_id = yt_infra.get_own_channel_id(youtube)
    _verify_expected_channel_id(ctx, args, channel_id)
    updated = yt_infra.update_channel_branding(youtube, channel_id, description=args.description)
    updated_channel = updated.get("brandingSettings", {}).get("channel", {})
    envelope = jsonout.build_envelope(run=ctx.run_header(), videos=[{
        "twitch_vod_id": None, "status": "uploaded", "reason_code": None,
        "youtube_channel_id": channel_id, "description": updated_channel.get("description"),
    }])
    return exitcodes.OK, envelope
