"""argparse tree for vodp (M1/M1改/M2/M3).

argparse's own ``parser.error()`` calls ``sys.exit(2)`` before any business
logic (and before a run_id/logger exist), which is exactly M4's ARG_ERROR
contract -- no JSON is emitted for those failures (see core/exitcodes.py).

Command dispatch keys are ``(args.command, args.subcommand)`` tuples; single
-level commands (``check``, ``whoami``) leave ``subcommand`` as ``None``.
"""

from __future__ import annotations

import argparse


PRIVACY_CHOICES = ("private", "unlisted", "public")


def _add_common(p: argparse.ArgumentParser) -> None:
    """Options accepted by every subcommand."""
    p.add_argument("--home", help="VODP_HOME override (highest priority)")
    p.add_argument("--profile", default="default", help="profiles/<name>.toml to use")
    p.add_argument(
        "--json", action="store_true",
        help="accepted for compatibility; JSON is always emitted on stdout (M8)",
    )
    p.add_argument(
        "--non-interactive", action="store_true",
        help="never open a browser for OAuth; fail fast with exit 4 instead",
    )
    p.add_argument("--ledger", help="ledger file path override (default: <home>/ledger/<channel_id>.jsonl)")


def _add_range(p: argparse.ArgumentParser, required: bool = False) -> None:
    p.add_argument(
        "--range", nargs=2, metavar=("START", "END"), required=required,
        help="date or datetime range, JST. Date-only expands to 00:00:00.000000-23:59:59.999999",
    )


def _add_meta_base(p: argparse.ArgumentParser) -> None:
    p.add_argument("--title", help="video title (overrides profile template)")
    p.add_argument("--description-file", help="path to a description template file")
    p.add_argument("--privacy", choices=PRIVACY_CHOICES, help="publish privacy status")
    p.add_argument("--tags", help="comma-separated tag list (overrides profile)")
    p.add_argument("--category-id", help="YouTube categoryId (overrides profile)")


_PLAYLIST_ID_HELP = (
    "use this existing playlist id directly (skips name lookup/creation; "
    "required if a by-name resolve would need to auto-create a playlist but "
    "the description is too close to the 5000-byte limit, M22)"
)


def _add_playlist_group(p: argparse.ArgumentParser, *, allow_name: bool) -> None:
    """``--playlist`` (by name, may create) and ``--playlist-id`` (must
    already exist) are mutually exclusive -- giving both would silently
    prefer one, which is exactly the kind of "accepted but ignored" flag
    combination that's worse than an upfront argument error."""
    if not allow_name:
        p.add_argument("--playlist-id", dest="playlist_id", help=_PLAYLIST_ID_HELP)
        return
    group = p.add_mutually_exclusive_group()
    group.add_argument("--playlist", help="playlist name to resolve/create and add the video to")
    group.add_argument("--playlist-id", dest="playlist_id", help=_PLAYLIST_ID_HELP)
    p.add_argument(
        "--playlist-privacy", choices=PRIVACY_CHOICES,
        help="privacy status if --playlist needs to create a new playlist (default: public)",
    )


def _add_dry_run(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dry-run", action="store_true", help="no DL/API-write/ledger-write/FS-write (M7)")


def _add_delete_guard(p: argparse.ArgumentParser) -> None:
    p.add_argument("--yes", action="store_true", help="required to actually execute a delete (default is dry-run)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vodp", description="Twitch VOD archive -> YouTube upload CLI")
    command_sub = parser.add_subparsers(dest="command", required=True)

    # --- vods --------------------------------------------------------
    vods = command_sub.add_parser("vods", help="Twitch VOD discovery and upload")
    vods_sub = vods.add_subparsers(dest="subcommand", required=True)

    vods_list = vods_sub.add_parser("list", help="list Twitch VODs in a range")
    _add_common(vods_list)
    _add_range(vods_list, required=True)

    vods_upload = vods_sub.add_parser("upload", help="download+upload+publish VODs in a range")
    _add_common(vods_upload)
    _add_range(vods_upload)
    _add_meta_base(vods_upload)
    _add_playlist_group(vods_upload, allow_name=True)
    _add_dry_run(vods_upload)
    vods_upload.add_argument("--max-uploads", type=int, default=5, help="cap on insert calls this run (default 5)")
    vods_upload.add_argument(
        "--deadline", type=float, default=3600.0,
        help="whole-run wall-clock budget in seconds for unattended runs (M15, default 3600)",
    )
    vods_upload.add_argument(
        "--allow-empty", action="store_true",
        help="exit 0 (instead of 3) when there are zero target VODs",
    )

    # --- videos --------------------------------------------------------
    videos = command_sub.add_parser("videos", help="YouTube video operations")
    videos_sub = videos.add_subparsers(dest="subcommand", required=True)

    videos_list = videos_sub.add_parser("list", help="list uploaded videos (uploads playlist)")
    _add_common(videos_list)

    videos_publish = videos_sub.add_parser("publish", help="update privacyStatus with readback verification")
    _add_common(videos_publish)
    _add_dry_run(videos_publish)
    videos_publish.add_argument("--video-id", required=True)
    videos_publish.add_argument("--privacy", choices=PRIVACY_CHOICES, required=True)

    videos_status = videos_sub.add_parser(
        "status", help="show uploadStatus/processingDetails for one or more videos (read-only)",
    )
    _add_common(videos_status)
    videos_status.add_argument(
        "--video-id", dest="video_ids", nargs="+", required=True,
        help="one or more YouTube video ids (space-separated)",
    )

    videos_delete = videos_sub.add_parser("delete", help="delete a video (default dry-run)")
    _add_common(videos_delete)
    _add_dry_run(videos_delete)
    _add_delete_guard(videos_delete)
    videos_delete.add_argument("--video-id", required=True)

    # --- meta --------------------------------------------------------
    meta = command_sub.add_parser("meta", help="video metadata operations")
    meta_sub = meta.add_subparsers(dest="subcommand", required=True)

    meta_set = meta_sub.add_parser("set", help="merge-update snippet metadata")
    _add_common(meta_set)
    _add_meta_base(meta_set)
    _add_playlist_group(meta_set, allow_name=False)  # meta set never resolves/creates by name
    _add_dry_run(meta_set)
    meta_set.add_argument("--video-id", required=True)

    # --- playlist --------------------------------------------------------
    playlist = command_sub.add_parser("playlist", help="playlist operations")
    playlist_sub = playlist.add_subparsers(dest="subcommand", required=True)

    playlist_list = playlist_sub.add_parser("list", help="list own playlists")
    _add_common(playlist_list)

    playlist_create = playlist_sub.add_parser("create", help="create a playlist")
    _add_common(playlist_create)
    _add_dry_run(playlist_create)
    playlist_create.add_argument("--name", required=True)
    playlist_create.add_argument("--privacy", choices=PRIVACY_CHOICES, default="public")

    playlist_add = playlist_sub.add_parser("add", help="add a video to a playlist (dedup by videoId)")
    _add_common(playlist_add)
    _add_dry_run(playlist_add)
    playlist_add.add_argument("--name", required=True)
    playlist_add.add_argument("--video-id", required=True)

    playlist_update = playlist_sub.add_parser(
        "update", help="update a playlist's title/description (merge-update, part=snippet)",
    )
    _add_common(playlist_update)
    _add_dry_run(playlist_update)
    playlist_update.add_argument("--name", required=True, help="current playlist title to resolve")
    playlist_update.add_argument("--new-title", help="new playlist title")
    playlist_update.add_argument(
        "--description", help="new playlist description (raw text, not a file path -- see meta set's --description-file for that)",
    )

    playlist_delete = playlist_sub.add_parser("delete", help="delete a playlist (default dry-run)")
    _add_common(playlist_delete)
    _add_dry_run(playlist_delete)
    _add_delete_guard(playlist_delete)
    playlist_delete.add_argument("--playlist-id", required=True)

    playlist_remove_item = playlist_sub.add_parser("remove-item", help="remove a video from a playlist (default dry-run)")
    _add_common(playlist_remove_item)
    _add_dry_run(playlist_remove_item)
    _add_delete_guard(playlist_remove_item)
    playlist_remove_item.add_argument("--playlist-id", required=True)
    playlist_remove_item.add_argument("--video-id", required=True)

    # --- channel --------------------------------------------------------
    channel = command_sub.add_parser("channel", help="YouTube channel metadata operations")
    channel_sub = channel.add_subparsers(dest="subcommand", required=True)

    channel_show = channel_sub.add_parser(
        "show", help="show the authenticated channel's title/description/customUrl (read-only, detailed whoami)",
    )
    _add_common(channel_show)

    # NOTE: no --title here -- channels.update rejects any actual title
    # change (channelTitleUpdateForbidden; renaming a channel is only
    # possible in YouTube Studio, not through this API at all), confirmed
    # against https://developers.google.com/youtube/v3/docs/channels/update.
    channel_update = channel_sub.add_parser(
        "update", help="update the channel description (merge-update, part=brandingSettings)",
    )
    _add_common(channel_update)
    _add_dry_run(channel_update)
    channel_update.add_argument("--description", required=True, help="new channel description")

    # --- cache --------------------------------------------------------
    cache = command_sub.add_parser("cache", help="local cache maintenance")
    cache_sub = cache.add_subparsers(dest="subcommand", required=True)

    cache_clean = cache_sub.add_parser("clean", help="remove orphaned downloads (default dry-run)")
    _add_common(cache_clean)
    _add_dry_run(cache_clean)
    _add_delete_guard(cache_clean)
    cache_clean.add_argument("--max-age-hours", type=float, default=24.0)
    cache_clean.add_argument(
        "--channel", dest="channel_id",
        help="channel id to check ledger state against before deleting a file "
             "(recommended; without it, only filename+mtime are used)",
    )

    # --- ledger --------------------------------------------------------
    ledger = command_sub.add_parser("ledger", help="ledger administration")
    ledger_sub = ledger.add_subparsers(dest="subcommand", required=True)

    ledger_recover = ledger_sub.add_parser("recover", help="list unresolved (inflight/recovery) ledger rows")
    _add_common(ledger_recover)
    ledger_recover.add_argument("--channel", required=True, dest="channel_id")

    ledger_repair = ledger_sub.add_parser("repair", help="quarantine a corrupt ledger file")
    _add_common(ledger_repair)
    ledger_repair.add_argument("--channel", required=True, dest="channel_id")

    ledger_resolve = ledger_sub.add_parser("resolve", help="resolve a recovery_required row")
    _add_common(ledger_resolve)
    ledger_resolve.add_argument("--channel", required=True, dest="channel_id")
    ledger_resolve.add_argument("--vod", required=True, dest="vod_id")
    ledger_resolve.add_argument("--action", choices=("reinsert", "abandon"), required=True)
    ledger_resolve.add_argument("--yes", action="store_true", required=True)

    # --- auth --------------------------------------------------------
    auth = command_sub.add_parser("auth", help="YouTube OAuth token management")
    auth_sub = auth.add_subparsers(dest="subcommand", required=True)

    auth_login = auth_sub.add_parser("login", help="interactive OAuth login")
    _add_common(auth_login)

    auth_migrate = auth_sub.add_parser("migrate", help="migrate legacy token.pickle to token.json")
    _add_common(auth_migrate)
    auth_migrate.add_argument("--pickle-path", help="path to legacy token.pickle")

    # --- launchd --------------------------------------------------------
    launchd = command_sub.add_parser("launchd", help="launchd integration")
    launchd_sub = launchd.add_subparsers(dest="subcommand", required=True)

    launchd_generate = launchd_sub.add_parser("generate", help="generate a launchd plist")
    _add_common(launchd_generate)
    launchd_generate.add_argument("--label", default="com.vodp.vods-upload")
    launchd_generate.add_argument("--venv-python", required=True, help="absolute path to the target .venv/bin/vodp")
    launchd_generate.add_argument("--working-directory", required=True)
    launchd_generate.add_argument("--out", help="output path (default: stdout)")

    # --- top level singletons --------------------------------------------
    check = command_sub.add_parser("check", help="read-only environment/config check")
    _add_common(check)
    _add_range(check)

    whoami = command_sub.add_parser("whoami", help="show the authenticated channel identity")
    _add_common(whoami)

    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "subcommand"):
        args.subcommand = None
    return args
