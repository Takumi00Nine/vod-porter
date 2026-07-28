"""``vodp vods list|upload`` (M1, M13, M14, M4, M7, M8, M15, M17, M22).

``vods upload`` is the whole pipeline: Twitch discovery -> preflight ->
download -> insert (private) -> readback-verified publish -> playlist ->
ledger. See docs/design.md §4 for the stage machine, §7 for the M22
description assembly order, and §9/S6 for what this step covers;
docs/design.md §11 (E2E, launchd, real credentials) is the lead's
responsibility, not this worker's.

Every non-idempotent call (videos.insert, playlists.insert,
playlistItems.insert) has exactly one call site that is allowed to invoke
it directly -- always immediately after this same call wrote the matching
``*_inflight`` stage itself, so "never attempted before" is guaranteed by
construction, not inferred. Resuming a row that was *already* at an
``*_inflight`` stage before this run started goes through a separate,
read-only reconciliation path (``_reconcile_*``) that never calls the
non-idempotent operation again -- only ``recovery_required`` if a listing
can't confirm it already happened.

M22 order of operations (design.md §7, "プレイリスト解決→insert"): the
target playlist is resolved (found, or created) exactly once per run,
*before* any video's download/insert, via ``_resolve_playlist_once`` --
its resulting URL is what gets baked into every video's description
before ``videos.insert`` is ever called. Adding each *video* to that
(already-resolved) playlist still happens after insert (a video obviously
can't be added to a playlist before it exists on YouTube).
"""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta
from pathlib import Path

import requests

from vodp.core import exitcodes, jsonout, reasons
from vodp.core.description import (
    build_description_for_insert,
    build_description_preview_for_new_playlist,
    check_new_playlist_description_budget,
    playlist_url_for,
)
from vodp.core.errors import ApiError, AuthRequiredError, EnvironmentError_, VodpError
from vodp.core.logging import JsonLineLogger
from vodp.core.preflight import PreflightInput, run_preflight
from vodp.core.profile import load_profile, verify_expected_channel
from vodp.core.template import render_template
from vodp.core.timerange import JST, parse_range
from vodp.infra import auth as auth_infra
from vodp.infra import downloader as downloader_infra
from vodp.infra import ffprobe as ffprobe_infra
from vodp.infra import youtube as yt_infra
from vodp.infra.deadline import Deadline, DeadlineExceededError
from vodp.infra.envconfig import load_env, require_twitch_config
from vodp.infra.ledger import Ledger, resolve_ledger_path
from vodp.infra.locking import LockAcquireError, exclusive_lock
from vodp.infra.retry import RetryBudget
from vodp.infra.signals import GracefulAbort, SignalController
from vodp.infra.twitch import TwitchClient

MAX_VIDEO_SECONDS = 43200  # 12h (YouTube limit for verified accounts)
MAX_VIDEO_BYTES = 256 * 1024**3  # 256GB
FOOTER_PREFIX = "[vodp:vod="
# A synthetic twitch_vod_id namespace for the channel-level "resolve/create
# this playlist" record, reusing the same exactly-once ledger machinery
# videos get (reserve/advance/expected_stage) instead of inventing a
# second persistence mechanism.
PLAYLIST_PSEUDO_PREFIX = "__playlist__:"


def _build_twitch_client(ctx, *, env=None, retry_budget=None, deadline=None) -> tuple[TwitchClient, str]:
    """``env`` may be pre-loaded by the caller (``run_upload`` needs it a
    moment earlier than this call itself, to register the client secret
    with the run's logger before anything else can possibly log it --
    R12, Codex/tester review round 2)."""
    if env is None:
        env = require_twitch_config(load_env(ctx.home))
    client = TwitchClient(
        env["TWITCH_CLIENT_ID"], env["TWITCH_CLIENT_SECRET"], requests.Session(),
        retry_budget=retry_budget, deadline=deadline,
    )
    return client, env["TWITCH_CHANNEL_LOGIN"]


def _default_range() -> tuple[datetime, datetime]:
    now_jst = datetime.now(JST)
    yesterday = (now_jst - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return yesterday, yesterday.replace(hour=23, minute=59, second=59, microsecond=999999)


def _split_by_earliest_date(candidates, profile):
    """The channel migration-day cutoff (``[limits] earliest_date``,
    M21改 §11) applied once, shared by both the dry-run preview and the
    real execution path -- a single source of truth instead of two
    independently-maintained filters that can (and did) silently drift
    apart. Real E2E testing found dry-run never applied this cutoff at
    all, so it "promised" a VOD the real run then silently, invisibly
    dropped -- violating M7's dry-run-is-a-complete-preview guarantee
    (team-lead E2E follow-up).

    Returns ``(kept, excluded)`` -- ``excluded`` is reported explicitly
    (``skipped``/``out_of_range``) rather than silently vanishing from
    ``videos[]``, in *both* paths alike."""
    if not profile.earliest_date:
        return list(candidates), []
    cutoff = datetime.fromisoformat(profile.earliest_date).replace(tzinfo=JST)
    kept = [v for v in candidates if v.created_at.astimezone(JST) >= cutoff]
    excluded = [v for v in candidates if v.created_at.astimezone(JST) < cutoff]
    return kept, excluded


def run_list(args, ctx):
    start, end = parse_range(*args.range) if args.range else _default_range()
    client, login = _build_twitch_client(ctx)
    user_id = client.get_user_id(login)
    result = client.list_videos(user_id, range_start=start)
    if not result.ok:
        raise ApiError(f"twitch VOD fetch incomplete/failed: {result.error}", reason_code=reasons.API_ERROR)

    in_range = [v for v in result.items if start <= v.created_at <= end]
    videos = [
        {
            "twitch_vod_id": v.id,
            "status": "skipped",
            "reason_code": None,
            "title": v.title,
            "created_at": v.created_at.isoformat(),
            "duration_seconds": v.duration_seconds,
            "url": v.url,
        }
        for v in in_range
    ]
    envelope = jsonout.build_envelope(
        run=ctx.run_header(), videos=videos,
        summary={"total": len(videos), "uploaded": 0, "skipped": len(videos), "failed": 0},
    )
    exit_code = exitcodes.OK if videos else exitcodes.NO_TARGETS
    return exit_code, envelope


class _VideoResult:
    def __init__(
        self, twitch_vod_id, status, reason_code=None, youtube_video_id=None, exit_priority=None, title=None,
    ):
        self.twitch_vod_id = twitch_vod_id
        self.status = status
        self.reason_code = reason_code
        self.youtube_video_id = youtube_video_id
        # M4 (Codex/tester review round 2, R3): the exit code *this one
        # result* would contribute to exitcodes.combine(). None means
        # "derive from status/reason_code the ordinary way" -- explicit
        # values are for results born from catching an ApiError/
        # EnvironmentError_, which must contribute 5 even though they're
        # reported as an ordinary per-video "failed" status, not silently
        # downgraded into the generic partial-failure bucket (6).
        self.exit_priority = exit_priority
        # Optional (team-lead E2E follow-up): only set for the
        # out_of_range informational entries below, so every other
        # existing call site's as_dict() output is unchanged.
        self.title = title

    def as_dict(self):
        d = {
            "twitch_vod_id": self.twitch_vod_id,
            "status": self.status,
            "reason_code": self.reason_code,
            "youtube_video_id": self.youtube_video_id,
        }
        if self.title is not None:
            d["title"] = self.title
        return d

    def default_exit_priority(self) -> int:
        if self.reason_code == reasons.RECOVERY_REQUIRED:
            return exitcodes.ENV_OR_API_ERROR
        if self.status == "failed":
            return exitcodes.PARTIAL_FAILURE
        return exitcodes.OK

    def exit_contribution(self) -> int:
        return self.exit_priority if self.exit_priority is not None else self.default_exit_priority()


class _InsertBudget:
    """Consumed only immediately before the actual (non-idempotent)
    videos.insert call -- not for videos that get skipped/rejected before
    ever reaching that point."""

    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0

    def exhausted(self) -> bool:
        return self.used >= self.limit

    def try_consume(self) -> bool:
        if self.exhausted():
            return False
        self.used += 1
        return True


def _safe_result(video_id, fn):
    """Isolate one video's failure from the rest of the batch: a
    preflight/API error for VOD A must not abort processing of VOD B.

    M4 (Codex/tester review round 2, R3): an ``ApiError``/
    ``EnvironmentError_`` is tagged with ``exit_priority=5`` explicitly --
    it's a genuine environment/API problem and must win priority over an
    ordinary per-video business failure (6) in the whole run's
    ``exitcodes.combine()``, even though it's reported the same way in
    the JSON (``status: "failed"``). A plain ``ArgError`` (content/
    preflight, e.g. title too long) stays an ordinary failure -- the CLI
    *arguments* were fine, so it must not bubble up as the CLI's own
    exit 2.

    ``AuthRequiredError`` (raised mid-run by infra/youtube.py's
    ``_execute()`` on a live HTTP 401 -- design.md §5 state I) is tagged
    ``exit_priority=4``, which *outranks* even the 5 an ``ApiError``
    result would get: per M4's 2>4>5>6>3>0, one video hitting a real
    "credentials no longer valid" condition must make the whole run
    report exit 4, not have that signal buried under some other video's
    lower-priority failure (R8, Codex/tester review round 2)."""
    try:
        return fn()
    except AuthRequiredError as e:
        return _VideoResult(video_id, "failed", e.reason_code or reasons.AUTH_REQUIRED,
                             exit_priority=exitcodes.AUTH_REQUIRED)
    except ApiError as e:
        return _VideoResult(video_id, "failed", e.reason_code or reasons.API_ERROR,
                             exit_priority=exitcodes.ENV_OR_API_ERROR)
    except EnvironmentError_ as e:
        return _VideoResult(video_id, "failed", e.reason_code or reasons.PREFLIGHT_FAILED,
                             exit_priority=exitcodes.ENV_OR_API_ERROR)
    except VodpError as e:
        return _VideoResult(video_id, "failed", e.reason_code or reasons.PREFLIGHT_FAILED)


def _find_video_id_by_footer(youtube, twitch_vod_id: str, *, retry_budget=None, deadline=None) -> str | None:
    """§4 C3: insert response lost -- check every uploaded video's
    description for our footer marker (exact last-line match, not a
    substring, and fails closed on more than one match) before giving up."""
    marker = f"{FOOTER_PREFIX}{twitch_vod_id}]"
    matches = []
    for item in yt_infra.list_my_uploaded_videos(youtube, retry_budget=retry_budget, deadline=deadline):
        video_id = item.get("contentDetails", {}).get("videoId")
        if not video_id:
            continue
        snippet = yt_infra.get_video_snippet(youtube, video_id, retry_budget=retry_budget, deadline=deadline)
        description = snippet.get("description") or ""
        last_line = description.replace("\r\n", "\n").rstrip().rsplit("\n", 1)[-1]
        if last_line == marker:
            matches.append(video_id)
    if len(matches) == 1:
        return matches[0]
    return None  # 0 or >1 matches: too ambiguous to trust, fail closed


def _resolve_playlist_once(ledger, youtube, settings, description_template, sample_values, sample_twitch_vod_id,
                            *, retry_budget=None, deadline=None):
    """M22: resolve (or create) the target playlist exactly once per run,
    *before* any insert. Cached via a synthetic ``__playlist__:<name>``
    ledger row so later videos in the same run (or a resumed run) reuse
    the result instead of re-resolving. Returns (playlist_id, playlist_url);
    (None, None) if no playlist was requested. Raises ArgError/
    EnvironmentError_ directly (deliberately *not* wrapped by
    ``_safe_result`` -- this is a whole-run configuration problem, not a
    single video's failure)."""
    if settings.playlist_id:
        return settings.playlist_id, playlist_url_for(settings.playlist_id)
    if not settings.playlist:
        return None, None

    pseudo_id = f"{PLAYLIST_PSEUDO_PREFIX}{settings.playlist}"
    current = ledger.get_current(pseudo_id)

    if current is not None and current["stage"] == "recovery_required":
        raise EnvironmentError_(
            f"playlist {settings.playlist!r} is in recovery_required from a previous run; "
            f"resolve via `vodp ledger resolve --vod {pseudo_id} --action reinsert|abandon --yes` first"
        )

    if current is None:
        ledger.reserve(
            twitch_vod_id=pseudo_id, twitch_user_id="__system__", profile="__system__",
            intent={"kind": "playlist", "playlist_name": settings.playlist},
        )
        current = ledger.get_current(pseudo_id)

    # Always re-confirm via the API this run, even if a previous run
    # already marked this pseudo row "completed" -- it's a write-ahead
    # guard against a double *create*, not a permanent unchecked cache.
    # The playlist can be renamed/deleted/recreated between runs (Codex
    # review, major #9), so trusting a stale id forever would keep
    # writing a dead URL into every future video's description.
    playlist = yt_infra.find_playlist_by_name(youtube, settings.playlist, retry_budget=retry_budget, deadline=deadline)
    if playlist is not None:
        if current["stage"] != "completed" or current.get("youtube_video_id") != playlist["id"]:
            ledger.advance(
                twitch_vod_id=pseudo_id, stage="completed", outcome="done",
                youtube_video_id=playlist["id"], expected_stage=current["stage"],
            )
        return playlist["id"], playlist_url_for(playlist["id"])

    if current["stage"] == "completed":
        # We previously created/found it, but it's gone under this name
        # now (deleted or renamed) -- fail closed rather than silently
        # create a second playlist under the same name.
        raise EnvironmentError_(
            f"playlist {settings.playlist!r} (previously id "
            f"{current.get('youtube_video_id')!r}) can no longer be found; pass --playlist-id "
            "or resolve manually before retrying"
        )

    if current["stage"] == "playlist_create_inflight":
        # A previous run wrote the write-ahead marker, but we just
        # confirmed (immediately above) that no such playlist exists now
        # -- ambiguous whether create was actually attempted. Fail closed
        # exactly like insert_inflight's treatment; never call create again.
        ledger.advance(
            twitch_vod_id=pseudo_id, stage="recovery_required",
            outcome="retryable_failure", expected_stage="playlist_create_inflight",
        )
        raise EnvironmentError_(
            f"playlist {settings.playlist!r} creation is in an unresolved state from a "
            f"previous run; resolve via `vodp ledger resolve --vod {pseudo_id} ...` first"
        )

    # Genuinely new: the orphan-playlist-prevention length check (M22)
    # builds the preview with a worst-case-length *placeholder* playlist
    # URL (not None) so it both estimates the byte budget accurately and
    # runs the real single-line/conflict validation up front (Codex
    # review, critical #2) -- the caller already picked the candidate
    # with the largest actual UTF-8 byte length, not just character count.
    preview_description = build_description_preview_for_new_playlist(
        description_template, sample_values, twitch_vod_id=sample_twitch_vod_id,
    )
    check_new_playlist_description_budget(preview_description)

    ledger.advance(twitch_vod_id=pseudo_id, stage="playlist_create_inflight", expected_stage=current["stage"])
    created = yt_infra.create_playlist(youtube, settings.playlist, settings.playlist_privacy)
    ledger.advance(
        twitch_vod_id=pseudo_id, stage="completed", outcome="done",
        youtube_video_id=created["id"], expected_stage="playlist_create_inflight",
    )
    return created["id"], playlist_url_for(created["id"])


def _add_to_playlist_first_attempt(ledger, youtube, twitch_vod_id, playlist_id, video_id,
                                    *, retry_budget=None, deadline=None) -> None:
    """Only ever called once per vod, immediately after ``meta_verified``
    is reached in this same call -- never on a resumed stale row, so the
    add call here is guaranteed never attempted before."""
    ledger.advance(twitch_vod_id=twitch_vod_id, stage="playlist_add_inflight", expected_stage="meta_verified")
    if not yt_infra.playlist_contains_video(youtube, playlist_id, video_id, retry_budget=retry_budget, deadline=deadline):
        yt_infra.add_video_to_playlist(youtube, playlist_id, video_id)


def _reconcile_playlist_add(ledger, youtube, entry, *, retry_budget=None, deadline=None) -> _VideoResult:
    """Resuming a row that was already at playlist_add_inflight before
    this run started: confirm membership via listing, recovery_required
    if that can't confirm success -- never call playlistItems.insert
    again blindly."""
    twitch_vod_id = entry["twitch_vod_id"]
    video_id = entry["youtube_video_id"]
    playlist_id = (entry.get("intent") or {}).get("playlist_id")

    def _recover():
        ledger.advance(
            twitch_vod_id=twitch_vod_id, stage="recovery_required",
            outcome="retryable_failure", expected_stage="playlist_add_inflight",
        )
        return _VideoResult(twitch_vod_id, "failed", reasons.RECOVERY_REQUIRED, video_id)

    if not playlist_id or not yt_infra.playlist_contains_video(
        youtube, playlist_id, video_id, retry_budget=retry_budget, deadline=deadline
    ):
        return _recover()
    ledger.advance(twitch_vod_id=twitch_vod_id, stage="playlist_added", expected_stage="playlist_add_inflight")
    ledger.advance(twitch_vod_id=twitch_vod_id, stage="completed", outcome="done", expected_stage="playlist_added")
    return _VideoResult(twitch_vod_id, "uploaded", None, video_id)


def _continue_post_upload(ledger, youtube, twitch_vod_id, *, retry_budget=None, deadline=None) -> _VideoResult:
    """Runs uploaded -> processed -> meta_verified -> (first-attempt add
    to the already-resolved playlist) -> completed. Only valid entry
    stages are uploaded/processing_pending/processed/privacy_lock/
    meta_verified/playlist_added -- anything else fails closed instead of
    silently reporting success."""
    current = ledger.get_current(twitch_vod_id)
    video_id = current["youtube_video_id"]
    intent = current["intent"]
    stage = current["stage"]

    if stage == "uploaded":
        status = yt_infra.get_video_status(youtube, video_id, retry_budget=retry_budget, deadline=deadline)
        upload_status = status.get("uploadStatus")
        if upload_status == "processed":
            ledger.advance(twitch_vod_id=twitch_vod_id, stage="processed", expected_stage="uploaded")
            stage = "processed"
        elif upload_status in ("uploaded", None):
            ledger.advance(twitch_vod_id=twitch_vod_id, stage="processing_pending",
                            outcome="retryable_failure", expected_stage="uploaded")
            return _VideoResult(twitch_vod_id, "skipped", reasons.PROCESSING_PENDING, video_id)
        else:
            ledger.advance(twitch_vod_id=twitch_vod_id, stage="rejected",
                            outcome="permanent_failure", expected_stage="uploaded")
            return _VideoResult(twitch_vod_id, "failed", reasons.REJECTED, video_id)

    if stage == "processing_pending":
        status = yt_infra.get_video_status(youtube, video_id, retry_budget=retry_budget, deadline=deadline)
        if status.get("uploadStatus") != "processed":
            return _VideoResult(twitch_vod_id, "skipped", reasons.PROCESSING_PENDING, video_id)
        ledger.advance(twitch_vod_id=twitch_vod_id, stage="processed", expected_stage="processing_pending")
        stage = "processed"

    if stage == "processed":
        yt_infra.publish_video(youtube, video_id, intent["privacy"], retry_budget=retry_budget, deadline=deadline)
        verified = yt_infra.verify_privacy(youtube, video_id, intent["privacy"],
                                            retry_budget=retry_budget, deadline=deadline)
        if not verified:
            ledger.advance(twitch_vod_id=twitch_vod_id, stage="privacy_lock",
                            outcome="retryable_failure", expected_stage="processed")
            return _VideoResult(twitch_vod_id, "failed", reasons.PRIVACY_LOCK, video_id)
        ledger.advance(twitch_vod_id=twitch_vod_id, stage="meta_verified", expected_stage="processed")
        stage = "meta_verified"

    if stage == "privacy_lock":
        verified = yt_infra.verify_privacy(youtube, video_id, intent["privacy"], attempts=1,
                                            retry_budget=retry_budget, deadline=deadline)
        if not verified:
            return _VideoResult(twitch_vod_id, "failed", reasons.PRIVACY_LOCK, video_id)
        ledger.advance(twitch_vod_id=twitch_vod_id, stage="meta_verified", expected_stage="privacy_lock")
        stage = "meta_verified"

    if stage == "meta_verified":
        added_stage_guard = "meta_verified"
        if intent.get("playlist_id"):
            _add_to_playlist_first_attempt(ledger, youtube, twitch_vod_id, intent["playlist_id"], video_id,
                                            retry_budget=retry_budget, deadline=deadline)
            added_stage_guard = "playlist_add_inflight"
        ledger.advance(twitch_vod_id=twitch_vod_id, stage="playlist_added", expected_stage=added_stage_guard)
        stage = "playlist_added"

    if stage == "playlist_added":
        ledger.advance(twitch_vod_id=twitch_vod_id, stage="completed", outcome="done", expected_stage="playlist_added")
        stage = "completed"

    if stage != "completed":
        # Reached via a stage this function doesn't know how to continue
        # from (e.g. playlist_add_inflight, which has its own dedicated
        # _reconcile_playlist_add) -- fail closed rather than silently
        # reporting "uploaded" for an unresolved row.
        raise EnvironmentError_(
            f"{twitch_vod_id}: _continue_post_upload cannot resume from stage {stage!r}"
        )
    return _VideoResult(twitch_vod_id, "uploaded", None, video_id)


def _process_fresh(ctx, ledger, youtube, twitch_vod_id, twitch_meta, budget: _InsertBudget, logger,
                    *, retry_budget=None, deadline=None) -> _VideoResult:
    """Handles a row whose current stage is ``reserved`` (brand new this
    run). Insert is only ever called from here, immediately after this
    same call wrote insert_inflight -- never on a resumed row."""
    current = ledger.get_current(twitch_vod_id)
    intent = current["intent"]
    ffprobe_path = shutil.which("ffprobe")
    run_preflight(PreflightInput(
        title=intent["title"], description=intent["description"], tags=intent["tags"],
        duration_seconds=twitch_meta.duration_seconds, download_dir=ctx.downloads_dir,
        ffprobe_path=ffprobe_path,
    ))

    logger.log("info", "download_start", twitch_vod_id=twitch_vod_id)
    file_path = downloader_infra.download_vod(
        twitch_meta.url, ctx.downloads_dir, twitch_vod_id, deadline=deadline,
    )
    try:
        # M15: ffprobe's subprocess timeout is bounded by whatever's left
        # of the whole-run deadline (capped at the usual 30s) instead of
        # always waiting the full 30s regardless of how little budget
        # remains. download_vod() above is now deadline-aware too (F6,
        # Codex/tester review round 2, differed-items follow-up: checked
        # on every yt-dlp progress_hook callback + yt-dlp's own
        # socket_timeout) -- the one remaining genuinely out-of-scope gap
        # is an interactive OAuth wait (see README).
        ffprobe_timeout = deadline.timeout_for(cap=30.0) if deadline is not None else 30.0
        actual_duration = ffprobe_infra.probe_duration_seconds(ffprobe_path, file_path, timeout=ffprobe_timeout)
        if actual_duration > MAX_VIDEO_SECONDS:
            ledger.advance(twitch_vod_id=twitch_vod_id, stage="rejected",
                            outcome="terminal_skip", expected_stage="reserved")
            return _VideoResult(twitch_vod_id, "skipped", reasons.TOO_LONG)
        size = file_path.stat().st_size
        if size > MAX_VIDEO_BYTES:
            ledger.advance(twitch_vod_id=twitch_vod_id, stage="rejected",
                            outcome="permanent_failure", expected_stage="reserved")
            return _VideoResult(twitch_vod_id, "failed", reasons.SIZE_EXCEEDED)

        if not budget.try_consume():
            return _VideoResult(twitch_vod_id, "skipped", reasons.MAX_UPLOADS)

        # write-ahead: this row is brand new (reserve() just created it) --
        # insert has never been attempted, so we call it directly, no
        # reconciliation needed.
        ledger.advance(twitch_vod_id=twitch_vod_id, stage="insert_inflight", expected_stage="reserved")
        media = _media_upload(file_path)
        video_id = yt_infra.insert_video(
            youtube, media_body=media, title=intent["title"],
            description=intent["description"], tags=intent["tags"], category_id=intent["category_id"],
            deadline=deadline,
        )
        ledger.advance(
            twitch_vod_id=twitch_vod_id, stage="uploaded", youtube_video_id=video_id,
            expected_stage="insert_inflight",
        )
    finally:
        file_path.unlink(missing_ok=True)

    logger.log("info", "upload_done", twitch_vod_id=twitch_vod_id, youtube_video_id=video_id)
    return _continue_post_upload(ledger, youtube, twitch_vod_id, retry_budget=retry_budget, deadline=deadline)


def _media_upload(file_path: Path):
    from googleapiclient.http import MediaFileUpload

    return MediaFileUpload(str(file_path), resumable=True)


def _reconcile_resumable(ledger, youtube, entry, *, retry_budget=None, deadline=None) -> _VideoResult:
    twitch_vod_id = entry["twitch_vod_id"]
    stage = entry["stage"]

    if stage == "insert_inflight":
        found = _find_video_id_by_footer(youtube, twitch_vod_id, retry_budget=retry_budget, deadline=deadline)
        if found is None:
            ledger.advance(
                twitch_vod_id=twitch_vod_id, stage="recovery_required",
                outcome="retryable_failure", expected_stage="insert_inflight",
            )
            return _VideoResult(twitch_vod_id, "failed", reasons.RECOVERY_REQUIRED)
        ledger.advance(
            twitch_vod_id=twitch_vod_id, stage="uploaded", youtube_video_id=found,
            expected_stage="insert_inflight",
        )
        return _continue_post_upload(ledger, youtube, twitch_vod_id, retry_budget=retry_budget, deadline=deadline)

    if stage == "playlist_add_inflight":
        return _reconcile_playlist_add(ledger, youtube, entry, retry_budget=retry_budget, deadline=deadline)

    # F1 (Codex/tester review round 2, differed-items follow-up): every
    # post-insert, pre-"completed" stage is resumable range-independently
    # now, not just the write-ahead "*_inflight" ones -- a crash-and-resume
    # across a range boundary (crashed day 1, the next unattended run's
    # default --range is day 2) must not strand this row forever just
    # because it's no longer rediscoverable via in-range candidate lookup.
    if stage in (
        "recovery_required", "privacy_lock", "processing_pending",
        "uploaded", "processed", "meta_verified", "playlist_added",
    ):
        if stage == "recovery_required":
            return _VideoResult(twitch_vod_id, "failed", reasons.RECOVERY_REQUIRED, entry.get("youtube_video_id"))
        return _continue_post_upload(ledger, youtube, twitch_vod_id, retry_budget=retry_budget, deadline=deadline)

    # Any other resumable stage vodp doesn't recognize a safe continuation
    # for -- fail closed instead of guessing.
    ledger.advance(twitch_vod_id=twitch_vod_id, stage="recovery_required", outcome="retryable_failure")
    return _VideoResult(twitch_vod_id, "failed", reasons.RECOVERY_REQUIRED, entry.get("youtube_video_id"))


def _read_description_file(path: str | None) -> str | None:
    if path is None:
        return None
    p = Path(path)
    if not p.is_file():
        raise EnvironmentError_(f"--description-file not found: {path}")
    return p.read_text(encoding="utf-8")


def run_upload(args, ctx):
    from vodp.cli.settings import resolve_settings

    profile = load_profile(ctx.home, args.profile)
    settings = resolve_settings(args, profile)
    description_file_content = _read_description_file(settings.description_file)

    if args.dry_run:
        # M7/M22: read-only preview -- no ledger/lock/download/insert/log
        # file. playlists.list is the *only* YouTube call allowed here
        # (M22 AC); it is itself gated by the normal auth state machine
        # (still exit 4 if no usable token -- dry-run never opens a
        # browser either, per §6).
        start, end = parse_range(*args.range) if args.range else _default_range()
        client, login = _build_twitch_client(ctx)
        user_id = client.get_user_id(login)
        result = client.list_videos(user_id, range_start=start)
        if not result.ok:
            raise ApiError(f"twitch VOD fetch incomplete/failed: {result.error}")
        in_range = [v for v in result.items if start <= v.created_at <= end]
        # team-lead E2E follow-up: dry-run must be a *complete* preview of
        # the real run (M7) -- it used to skip this cutoff entirely,
        # so it could "promise" a VOD the real run then silently dropped.
        candidates, out_of_range = _split_by_earliest_date(in_range, profile)

        playlist_url = None
        planned_playlist_title = None
        if settings.playlist_id:
            playlist_url = playlist_url_for(settings.playlist_id)
        elif settings.playlist:
            creds = auth_infra.authenticate(ctx.home, non_interactive=True, dry_run=True)
            youtube = yt_infra.build_client(creds)
            found = yt_infra.find_playlist_by_name(youtube, settings.playlist)
            if found is not None:
                playlist_url = playlist_url_for(found["id"])
            else:
                planned_playlist_title = settings.playlist

        videos = [
            {
                "twitch_vod_id": v.id, "status": "skipped", "reason_code": reasons.DRY_RUN, "title": v.title,
                "playlist_url": playlist_url, "planned_playlist_title": planned_playlist_title,
            }
            for v in candidates
        ] + [
            # Explicit, not silently dropped -- the real execution path
            # reports the exact same set of exclusions the exact same way.
            {"twitch_vod_id": v.id, "status": "skipped", "reason_code": reasons.OUT_OF_RANGE, "title": v.title}
            for v in out_of_range
        ]
        envelope = jsonout.build_envelope(run=ctx.run_header(), videos=videos)
        # NO_TARGETS is about *actionable* candidates -- an out_of_range
        # video was never a real target for this run to begin with, so
        # its presence alone must not turn what would otherwise be "found
        # nothing to do" into a false "OK, 0 uploaded".
        exit_code = exitcodes.OK if candidates or settings.allow_empty else exitcodes.NO_TARGETS
        return exit_code, envelope

    logger = JsonLineLogger(ctx.home, ctx.run_id)
    ctx.logger = logger
    try:
        start, end = parse_range(*args.range) if args.range else _default_range()

        # M15: a single retry budget (idempotent GET/list/update only) and
        # deadline shared across every Twitch/YouTube call this run makes.
        deadline = Deadline(total_seconds=settings.deadline_seconds)
        retry_budget = RetryBudget()

        # M19 AC (requirements.md M19: sentinel secret must never appear
        # in stdout/stderr/the log file): register every secret this run
        # holds with ctx (which forwards to both main.py's stderr/JSON
        # redaction and logger's file redaction) *before* any call that
        # could possibly surface an error containing one -- registering
        # them only at the very end (or never, as before this fix, and
        # only into the logger, never stdout/stderr, until the Codex
        # light-review follow-up) would leave a window where a real
        # failure's message leaks a raw secret.
        twitch_env = require_twitch_config(load_env(ctx.home))
        ctx.register_secrets([twitch_env.get("TWITCH_CLIENT_SECRET")])
        twitch_client, login = _build_twitch_client(
            ctx, env=twitch_env, retry_budget=retry_budget, deadline=deadline,
        )
        try:
            user_id = twitch_client.get_user_id(login)
        finally:
            # F4 (Codex/tester review round 2, differed-items follow-up;
            # Codex light review, round-3 follow-up): the minted Twitch
            # bearer access token is just as sensitive as the client
            # secret it came from -- register it in a ``finally`` so it's
            # masked even if get_user_id() itself raises (e.g. a 401 on
            # the very next retry, or the /users lookup failing) *after*
            # TwitchClient already minted one via get_app_token() --
            # registering only on the success path left a window where
            # an already-fetched token could appear, unmasked, in that
            # failure's own message. ``register_secrets`` already no-ops
            # on ``None`` (token never fetched at all).
            ctx.register_secrets([twitch_client.token])

        creds = auth_infra.authenticate(ctx.home, non_interactive=args.non_interactive)
        ctx.register_secrets([creds.token, creds.refresh_token, creds.client_secret])
        youtube = yt_infra.build_client(creds)
        # Routed through the shared retry_budget/deadline like every other
        # idempotent read this run makes, and any surviving HttpError is
        # already normalized to ApiError by get_own_channel_id (Codex
        # review, major #8).
        channel_id = yt_infra.get_own_channel_id(youtube, retry_budget=retry_budget, deadline=deadline)
        # F5 (Codex/tester review round 2, differed-items follow-up,
        # requirements.md 推奨S7): refuse to touch the wrong YouTube
        # account -- no-op unless the profile actually sets
        # expected_channel_id.
        verify_expected_channel(profile.expected_channel_id, channel_id)

        channel_lock_path = ctx.run_dir / "channels" / f"{channel_id}.lock"
        try:
            lock_ctx = exclusive_lock(channel_lock_path)
            lock_ctx.__enter__()
        except LockAcquireError as e:
            raise EnvironmentError_(f"another vodp run holds the lock for channel {channel_id}: {e}") from e

        try:
            ledger_path = resolve_ledger_path(ctx.home, channel_id, override=args.ledger)
            ledger = Ledger(ledger_path, channel_id=channel_id)

            results: list[_VideoResult] = []
            # Informational only (team-lead E2E follow-up): out_of_range
            # entries -- reported in videos[] for transparency, but never
            # counted towards exit-code aggregation or the NO_TARGETS
            # check below (an out_of_range video was never a real target).
            informational_results: list[_VideoResult] = []
            interrupted = False

            with SignalController() as signal_ctrl:
                try:
                    # 1. range-independent resume queue, before any new
                    # discovery (§4). Synthetic playlist pseudo-rows are
                    # handled by _resolve_playlist_once, not here.
                    for entry in ledger.resumable_entries():
                        if entry["twitch_vod_id"].startswith(PLAYLIST_PSEUDO_PREFIX):
                            continue
                        if signal_ctrl.interrupted:
                            break
                        deadline.check()
                        results.append(_safe_result(
                            entry["twitch_vod_id"], lambda e=entry: _reconcile_resumable(
                                ledger, youtube, e, retry_budget=retry_budget, deadline=deadline,
                            )
                        ))
                        if results[-1].exit_priority == exitcodes.AUTH_REQUIRED:
                            # Codex light review (round-2 follow-up on
                            # R8): once one call has confirmed the
                            # credentials are actually rejected (not a
                            # transient/per-resource problem), every
                            # remaining candidate would just fail the
                            # exact same way -- stop spending API calls
                            # and time on a run that can only end in 4.
                            break

                    handled_ids = {r.twitch_vod_id for r in results}
                    has_recovery_required = any(r.reason_code == reasons.RECOVERY_REQUIRED for r in results)
                    has_auth_required = any(r.exit_priority == exitcodes.AUTH_REQUIRED for r in results)

                    # A recovery_required row blocks *new* work for this
                    # channel entirely -- fail closed rather than let new
                    # inserts happen while an earlier one is unresolved.
                    # Likewise, once the credentials themselves are
                    # confirmed rejected (401 mid-run), fresh discovery
                    # would just fail every candidate the exact same way
                    # (Codex light review, round-2 follow-up on R8).
                    if not has_recovery_required and not has_auth_required:
                        fetch = twitch_client.list_videos(user_id, range_start=start)
                        if not fetch.ok:
                            raise ApiError(f"twitch VOD fetch incomplete/failed: {fetch.error}")
                        in_range = [v for v in fetch.items if start <= v.created_at <= end]
                        candidates, out_of_range = _split_by_earliest_date(in_range, profile)
                        # team-lead E2E follow-up: report the exclusion
                        # explicitly (same reason_code dry-run now uses)
                        # instead of silently vanishing from videos[] --
                        # kept out of ``results`` itself so it never
                        # affects exit-code aggregation (an out_of_range
                        # video was never a real target to begin with).
                        informational_results.extend(
                            _VideoResult(v.id, "skipped", reasons.OUT_OF_RANGE, title=v.title) for v in out_of_range
                        )
                        # Never reprocess a VOD already handled by the resume queue above.
                        candidates = [v for v in candidates if v.id not in handled_ids]

                        description_template = (
                            description_file_content if description_file_content is not None
                            else profile.description_template
                        )

                        # M22: resolve the playlist exactly once, before
                        # any insert this run -- deliberately NOT wrapped
                        # in _safe_result, so an orphan-prevention/
                        # recovery_required failure here aborts the whole
                        # run (exit 2/5) instead of being absorbed as one
                        # video's failure.
                        playlist_id = playlist_url = None
                        if candidates and (settings.playlist or settings.playlist_id):
                            # Worst case by actual UTF-8 *byte* length, not
                            # character count -- a short multi-byte (e.g.
                            # emoji-heavy) title can outweigh a longer
                            # plain-ASCII one (Codex review, critical #2).
                            sample = max(candidates, key=lambda v: len((v.title or "").encode("utf-8")))
                            sample_values = {
                                "date": sample.created_at.astimezone(JST).strftime("%Y-%m-%d"),
                                "vod_url": sample.url, "title": sample.title,
                            }
                            playlist_id, playlist_url = _resolve_playlist_once(
                                ledger, youtube, settings, description_template, sample_values, sample.id,
                                retry_budget=retry_budget, deadline=deadline,
                            )

                        budget = _InsertBudget(settings.max_uploads)
                        for video in candidates:
                            if signal_ctrl.interrupted:
                                break
                            deadline.check()
                            results.append(_safe_result(video.id, lambda v=video: _process_one_candidate(
                                ctx, ledger, youtube, v, user_id, profile, settings,
                                description_template, args.profile, budget, logger, playlist_id, playlist_url,
                                retry_budget=retry_budget, deadline=deadline,
                            )))
                            if results[-1].exit_priority == exitcodes.AUTH_REQUIRED:
                                break  # credentials confirmed rejected -- stop trying more videos
                except (DeadlineExceededError, GracefulAbort) as e:
                    # M15: every result gathered so far is already durably
                    # committed to the ledger (each stage transition fsyncs
                    # before returning) -- we just stop taking on new work
                    # and report exit 5 with whatever we have.
                    logger.log("warning", "run_interrupted", reason=str(e))
                    interrupted = True

            all_results = results + informational_results
            videos = [r.as_dict() for r in all_results]
            summary = {
                "total": len(videos),
                "uploaded": sum(1 for r in all_results if r.status == "uploaded"),
                "skipped": sum(1 for r in all_results if r.status == "skipped"),
                "failed": sum(1 for r in all_results if r.status == "failed"),
            }
            envelope = jsonout.build_envelope(run=ctx.run_header(), videos=videos, summary=summary)

            # M4 (Codex/tester review round 2, R3): each result carries its
            # own natural exit-code contribution (e.g. an ApiError-tagged
            # failure is 5, an ordinary business "failed" is 6) -- a flat
            # has_failed/has_recovery_required if/elif chain would silently
            # collapse a real environment/API failure into the generic
            # partial-failure code. exitcodes.combine() applies the single
            # documented priority (2>4>5>6>3>0) over every candidate at once.
            #
            # Deliberately computed from ``results`` alone, not
            # ``all_results``/``videos`` (team-lead E2E follow-up): an
            # out_of_range video was never a real target for this run, so
            # its (purely informational) presence must never turn what
            # would otherwise be "found nothing to do" (NO_TARGETS) into
            # a false "OK, 0 uploaded".
            candidate_codes = [exitcodes.ENV_OR_API_ERROR] if interrupted else []
            candidate_codes.extend(r.exit_contribution() for r in results)
            if not results and not settings.allow_empty:
                candidate_codes.append(exitcodes.NO_TARGETS)
            if not candidate_codes:
                candidate_codes.append(exitcodes.OK)
            return exitcodes.combine(*candidate_codes), envelope
        finally:
            lock_ctx.__exit__(None, None, None)
    finally:
        logger.purge_old()


def _process_one_candidate(ctx, ledger, youtube, video, twitch_user_id, profile, settings,
                            description_template, profile_name, budget: _InsertBudget, logger,
                            playlist_id, playlist_url, *, retry_budget=None, deadline=None) -> _VideoResult:
    """Pre-reservation checks (duration known/within limits, preflight)
    happen *before* ``ledger.reserve()`` so a preflight failure never
    leaves a stuck reservation that a corrected retry can't get past via
    an intent-hash mismatch. ``playlist_id``/``playlist_url`` are already
    resolved (once, before the loop) by the caller."""
    if video.duration_seconds is None:
        return _VideoResult(video.id, "failed", reasons.DURATION_UNKNOWN)
    if video.duration_seconds > MAX_VIDEO_SECONDS:
        return _VideoResult(video.id, "skipped", reasons.TOO_LONG)

    values = {
        "date": video.created_at.astimezone(JST).strftime("%Y-%m-%d"),
        "vod_url": video.url,
        "title": video.title,
    }
    title = settings.title or render_template(profile.title_template, {**values, "playlist_url": playlist_url or ""})
    description = build_description_for_insert(
        description_template, values, twitch_vod_id=video.id, playlist_url=playlist_url,
    )

    intent = {
        "title": title, "description": description, "tags": settings.tags,
        "category_id": settings.category_id, "privacy": settings.privacy,
        "playlist_id": playlist_id,
    }

    run_preflight(PreflightInput(
        title=title, description=description, tags=settings.tags,
        duration_seconds=video.duration_seconds, download_dir=ctx.downloads_dir,
        ffprobe_path=shutil.which("ffprobe"),
    ))

    reserved = ledger.reserve(
        twitch_vod_id=video.id, twitch_user_id=twitch_user_id, profile=profile_name, intent=intent,
    )
    if reserved.action == "skip_done":
        return _VideoResult(video.id, "skipped", reasons.ALREADY_UPLOADED, reserved.record.get("youtube_video_id"))
    if reserved.record.get("stage") != "reserved":
        # Resuming mid-pipeline for a VOD that's within *this* run's range
        # (not caught by the resume-queue snapshot because it never left
        # "reserved" before -- e.g. crashed between reserve() and the
        # duration/preflight checks). Continue forward from wherever it is.
        return _continue_post_upload(ledger, youtube, video.id, retry_budget=retry_budget, deadline=deadline)
    return _process_fresh(ctx, ledger, youtube, video.id, video, budget, logger,
                           retry_budget=retry_budget, deadline=deadline)
