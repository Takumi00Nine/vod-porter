"""YouTube Data API v3 wrapper (M10/M11/M12, M1改 videos list/meta set).

``build`` is re-exported at module scope specifically so tests can
monkeypatch ``vodp.infra.youtube.build`` with a FakeYouTube factory
(design.md §10) instead of ever constructing a real HTTP transport.

M15 retry policy: every read (``videos.list``/``playlists.list``/
``playlistItems.list``/``channels.list``) and ``videos.update`` accept an
optional ``retry_budget`` (``vodp.infra.retry.RetryBudget``) shared across
a whole run -- passing one in wraps the call with finite exponential-
backoff retry on 429/5xx. The three non-idempotent writes
(``videos.insert``, ``playlists.insert``, ``playlistItems.insert``) never
take a ``retry_budget`` parameter at all and always execute with
``num_retries=0`` explicitly, so a second attempt can only ever happen
through the ledger's write-ahead + reconciliation path, never here.
"""

from __future__ import annotations

import time

from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build  # noqa: F401 - patched in tests
from googleapiclient.errors import HttpError

from vodp.core.errors import ApiError, AuthRequiredError
from vodp.infra.retry import RetryBudget, call_with_retry, is_retryable_status

PRIVACY_UPDATE_ALLOWLIST = (
    "privacyStatus",
    "embeddable",
    "license",
    "publicStatsViewable",
    "publishAt",
    "selfDeclaredMadeForKids",
)


def build_client(credentials):
    return build("youtube", "v3", credentials=credentials, cache_discovery=False)


def _is_retryable_http_error(exc: Exception) -> bool:
    if not isinstance(exc, HttpError):
        return False
    status = getattr(getattr(exc, "resp", None), "status", None)
    return is_retryable_status(status)


def _convert_http_error(e: HttpError | RefreshError) -> Exception:
    """Single conversion rule shared by every call site (``_execute()``
    *and* the non-idempotent/delete calls below that never take a
    ``retry_budget`` at all): a 401 means the credentials themselves got
    rejected *during use* (design.md §5 state I) -- ``AuthRequiredError``
    (4), never retried, never auto-re-authenticated. Everything else is
    the generic ``ApiError`` (5). Originally only wired into
    ``_execute()``, which left every non-idempotent write (insert_video's
    chunk loop, create_playlist, add_video_to_playlist, the three
    delete_* calls) able to leak a raw, unconverted ``HttpError`` past
    per-video isolation (Codex light review, round-2 follow-up).

    A surviving ``RefreshError`` (google-auth raises this, not an
    ``HttpError``, when googleapiclient's transport tries to silently
    refresh an expired access token mid-call and the refresh itself is
    rejected -- e.g. a revoked refresh_token/invalid_grant) is the exact
    same state I condition as a live 401 and gets the same
    ``AuthRequiredError`` treatment (F3, Codex/tester review round 2,
    differed-items follow-up: HTTP 401 alone missed this path entirely,
    since it isn't an HttpError at all)."""
    if isinstance(e, RefreshError):
        return AuthRequiredError(
            f"YouTube API call's credential refresh was rejected (invalid_grant or similar): {e}; "
            "run `vodp auth login` again"
        )
    status = getattr(getattr(e, "resp", None), "status", None)
    if status == 401:
        return AuthRequiredError(
            f"YouTube API call rejected the current credentials (HTTP 401): {e}; "
            "run `vodp auth login` again"
        )
    return ApiError(f"YouTube API call failed: HTTP {status}: {e}")


def _execute(request, *, retry_budget: RetryBudget | None = None, deadline=None):
    """Idempotent-call execute(), optionally wrapped in finite retry.

    Always converts a surviving ``HttpError``/``RefreshError`` (via
    ``_convert_http_error``) -- with or without a retry budget -- so
    every caller (including per-video isolation in
    ``cli/commands/vods.py``'s ``_safe_result``, which only catches
    ``VodpError``/``ApiError``/``AuthRequiredError``) sees a consistent
    exception type instead of an unconverted googleapiclient/google-auth
    exception leaking through and aborting the whole batch (Codex
    review, major #8)."""
    try:
        if retry_budget is None:
            return request.execute()
        return call_with_retry(
            request.execute, budget=retry_budget, is_retryable=_is_retryable_http_error, deadline=deadline,
        )
    except (HttpError, RefreshError) as e:
        raise _convert_http_error(e) from e


def _execute_once(request, **execute_kwargs):
    """For calls that never take a ``retry_budget`` at all (the three
    non-idempotent writes, and the delete_* calls below -- deleting
    twice is not safely repeatable either) -- still normalizes a
    surviving ``HttpError``/``RefreshError`` the same way ``_execute()``
    does, instead of leaking a raw exception past per-video isolation."""
    try:
        return request.execute(**execute_kwargs)
    except (HttpError, RefreshError) as e:
        raise _convert_http_error(e) from e


# -- upload (M10) -------------------------------------------------------------

def insert_video(
    youtube, *, media_body, title: str, description: str, tags: list[str], category_id: str, deadline=None,
) -> str:
    """Full snippet+status insert, privacyStatus always 'private' (M10).
    Never retried (num_retries=0, no retry_budget parameter at all) --
    this is one of the three non-idempotent calls M15/§4 forbid retrying.

    ``deadline`` (optional) is checked at each chunk boundary -- once a
    chunk upload is in flight it can't be aborted mid-transfer, but the
    *next* chunk is never started once the run's deadline has passed
    (design.md §8: "upload は chunk 境界で中断判定")."""
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": "private",
            "selfDeclaredMadeForKids": False,
        },
    }
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media_body)
    response = None
    while response is None:
        if deadline is not None:
            deadline.check()
        try:
            _status, response = request.next_chunk(num_retries=0)
        except (HttpError, RefreshError) as e:
            # Same normalization _execute() applies -- a 401 or a failed
            # silent token refresh here means the credentials were
            # rejected mid-upload; never leak the raw exception past
            # per-video isolation (Codex light review, round-2 follow-up,
            # extended in F3 to cover RefreshError too).
            raise _convert_http_error(e) from e
    return response["id"]


def get_video_status(youtube, video_id: str, *, retry_budget: RetryBudget | None = None, deadline=None) -> dict:
    resp = _execute(youtube.videos().list(part="status", id=video_id), retry_budget=retry_budget, deadline=deadline)
    items = resp.get("items", [])
    if not items:
        raise ApiError(f"video {video_id} not found (status)")
    return items[0]["status"]


def get_video_snippet(youtube, video_id: str, *, retry_budget: RetryBudget | None = None, deadline=None) -> dict:
    resp = _execute(youtube.videos().list(part="snippet", id=video_id), retry_budget=retry_budget, deadline=deadline)
    items = resp.get("items", [])
    if not items:
        raise ApiError(f"video {video_id} not found (snippet)")
    return items[0]["snippet"]


# -- publish (M11): read -> allowlist merge -> update(part=status) only ------

def publish_video(
    youtube, video_id: str, privacy_status: str, *, retry_budget: RetryBudget | None = None, deadline=None
) -> dict:
    current = get_video_status(youtube, video_id, retry_budget=retry_budget, deadline=deadline)
    merged = {k: v for k, v in current.items() if k in PRIVACY_UPDATE_ALLOWLIST}
    merged["privacyStatus"] = privacy_status
    # videos.update is idempotent (same body -> same result) -- retry allowed.
    return _execute(
        youtube.videos().update(part="status", body={"id": video_id, "status": merged}),
        retry_budget=retry_budget, deadline=deadline,
    )


def verify_privacy(
    youtube, video_id: str, expected_privacy: str, *, attempts: int = 5, delay: float = 6.0, sleep=None,
    retry_budget: RetryBudget | None = None, deadline=None,
) -> bool:
    """M11: up to 5 polls / ~30s total; True once the readback matches.

    ``sleep`` defaults to ``None`` and resolves to ``time.sleep`` at call
    time (not at function-definition time) so tests can monkeypatch
    ``time.sleep`` globally instead of needing to thread a fake through
    every command layer.
    """
    sleep_fn = sleep if sleep is not None else time.sleep
    for attempt in range(attempts):
        status = get_video_status(youtube, video_id, retry_budget=retry_budget, deadline=deadline)
        if status.get("privacyStatus") == expected_privacy:
            return True
        if attempt < attempts - 1:
            sleep_fn(delay)
    return False


# -- videos list (M1改: uploads playlist, never search.list) ------------------

def get_own_channel_id(youtube, *, retry_budget: RetryBudget | None = None, deadline=None) -> str:
    resp = _execute(youtube.channels().list(part="id", mine=True), retry_budget=retry_budget, deadline=deadline)
    items = resp.get("items", [])
    if not items:
        raise ApiError("no channel found for the authenticated account (channels.list mine=true)")
    return items[0]["id"]


def get_own_channel_snippet(youtube, *, retry_budget: RetryBudget | None = None, deadline=None) -> dict:
    """``whoami``'s own need (id + title) -- routed through ``_execute()``
    like every other read (Codex/tester review round 2, differed-items
    follow-up F2: ``whoami`` used to call ``.execute()`` directly,
    leaking a raw, unconverted ``HttpError`` past main.py's VodpError
    handling on any failure -- including a 401, which should surface as
    AuthRequiredError(4) same as everywhere else, not an unhandled
    exception falling through to the generic exit-5 defensive branch)."""
    resp = _execute(youtube.channels().list(part="snippet", mine=True), retry_budget=retry_budget, deadline=deadline)
    items = resp.get("items", [])
    if not items:
        raise ApiError("no channel found for the authenticated account (channels.list mine=true)")
    return items[0]


def get_own_channel_detail(youtube, *, retry_budget: RetryBudget | None = None, deadline=None) -> dict:
    """``channel show``'s read-only need: id + snippet (title/description/
    customUrl) + brandingSettings in one call -- a superset of
    ``get_own_channel_snippet()`` (whoami keeps using the cheaper
    part=snippet-only call; this one exists for the detailed view)."""
    resp = _execute(
        youtube.channels().list(part="snippet,brandingSettings", mine=True),
        retry_budget=retry_budget, deadline=deadline,
    )
    items = resp.get("items", [])
    if not items:
        raise ApiError("no channel found for the authenticated account (channels.list mine=true)")
    return items[0]


def get_uploads_playlist_id(youtube, *, retry_budget: RetryBudget | None = None, deadline=None) -> str:
    resp = _execute(
        youtube.channels().list(part="contentDetails", mine=True), retry_budget=retry_budget, deadline=deadline,
    )
    items = resp.get("items", [])
    if not items:
        raise ApiError("no channel found for the authenticated account (channels.list mine=true)")
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]


def list_playlist_items(
    youtube, playlist_id: str, *, part: str = "snippet,contentDetails",
    retry_budget: RetryBudget | None = None, deadline=None,
) -> list[dict]:
    items: list[dict] = []
    page_token = None
    while True:
        params = {"part": part, "playlistId": playlist_id, "maxResults": 50}
        if page_token:
            params["pageToken"] = page_token
        resp = _execute(youtube.playlistItems().list(**params), retry_budget=retry_budget, deadline=deadline)
        items.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            return items


def list_my_uploaded_videos(youtube, *, retry_budget: RetryBudget | None = None, deadline=None) -> list[dict]:
    uploads_id = get_uploads_playlist_id(youtube, retry_budget=retry_budget, deadline=deadline)
    return list_playlist_items(youtube, uploads_id, retry_budget=retry_budget, deadline=deadline)


# -- playlists (M12) -----------------------------------------------------------

def list_my_playlists(youtube, *, retry_budget: RetryBudget | None = None, deadline=None) -> list[dict]:
    items: list[dict] = []
    page_token = None
    while True:
        params = {"part": "snippet", "mine": True, "maxResults": 50}
        if page_token:
            params["pageToken"] = page_token
        resp = _execute(youtube.playlists().list(**params), retry_budget=retry_budget, deadline=deadline)
        items.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            return items


def find_playlist_by_name(
    youtube, name: str, *, retry_budget: RetryBudget | None = None, deadline=None
) -> dict | None:
    matches = [
        p for p in list_my_playlists(youtube, retry_budget=retry_budget, deadline=deadline)
        if p["snippet"]["title"] == name
    ]
    if len(matches) >= 2:
        raise ApiError(f"{len(matches)} playlists named {name!r} found; refusing to guess which one")
    return matches[0] if matches else None


def create_playlist(youtube, name: str, privacy: str) -> dict:
    """Non-idempotent (playlists.insert): never retried, no retry_budget
    parameter at all -- num_retries=0 explicit."""
    body = {"snippet": {"title": name}, "status": {"privacyStatus": privacy}}
    return _execute_once(youtube.playlists().insert(part="snippet,status", body=body), num_retries=0)


def playlist_contains_video(
    youtube, playlist_id: str, video_id: str, *, retry_budget: RetryBudget | None = None, deadline=None
) -> bool:
    items = list_playlist_items(youtube, playlist_id, part="contentDetails",
                                 retry_budget=retry_budget, deadline=deadline)
    return any(it["contentDetails"]["videoId"] == video_id for it in items)


def add_video_to_playlist(youtube, playlist_id: str, video_id: str) -> dict:
    """Non-idempotent (playlistItems.insert): never retried, no
    retry_budget parameter at all -- num_retries=0 explicit."""
    body = {
        "snippet": {
            "playlistId": playlist_id,
            "resourceId": {"kind": "youtube#video", "videoId": video_id},
        }
    }
    return _execute_once(youtube.playlistItems().insert(part="snippet", body=body), num_retries=0)


def delete_playlist(youtube, playlist_id: str) -> None:
    _execute_once(youtube.playlists().delete(id=playlist_id))


def remove_playlist_item(youtube, playlist_item_id: str) -> None:
    _execute_once(youtube.playlistItems().delete(id=playlist_item_id))


# -- playlist update (``vodp playlist update``: read -> merge -> update, ----
# never drops title/description the caller didn't touch, mirrors update_snippet
# below for videos)

def get_playlist_snippet(youtube, playlist_id: str, *, retry_budget: RetryBudget | None = None, deadline=None) -> dict:
    resp = _execute(
        youtube.playlists().list(part="snippet", id=playlist_id), retry_budget=retry_budget, deadline=deadline,
    )
    items = resp.get("items", [])
    if not items:
        raise ApiError(f"playlist {playlist_id} not found (snippet)")
    return items[0]["snippet"]


def merge_playlist_snippet(current: dict, *, title=None, description=None) -> dict:
    merged = dict(current)
    if title is not None:
        merged["title"] = title
    if description is not None:
        merged["description"] = description
    return merged


def update_playlist_snippet(
    youtube, playlist_id: str, *, current_snippet: dict | None = None,
    retry_budget: RetryBudget | None = None, deadline=None, **overrides
) -> dict:
    """``current_snippet``: pass in an already-fetched snippet to avoid a
    redundant second ``playlists.list`` call (same rationale as
    ``update_snippet`` below for videos)."""
    current = current_snippet if current_snippet is not None else get_playlist_snippet(
        youtube, playlist_id, retry_budget=retry_budget, deadline=deadline,
    )
    merged = merge_playlist_snippet(current, **overrides)
    # title is a required API field on part=snippet -- always re-send it
    # even when this call only touches description (罠4: playlists.update
    # replaces the whole snippet part, not just the given fields;
    # Knowledge/youtube-api-pitfalls.md). status is never touched here
    # (part=snippet only).
    merged.setdefault("title", current.get("title"))
    # playlists.update is idempotent -- retry allowed.
    return _execute(
        youtube.playlists().update(part="snippet", body={"id": playlist_id, "snippet": merged}),
        retry_budget=retry_budget, deadline=deadline,
    )


# -- channel update (``vodp channel update``: read -> merge -> update, ------
# same merge-update shape as playlist update above, but channels.update
# only accepts part=brandingSettings (never part=snippet -- snippet.title/
# description are read-only to API consumers; confirmed against
# https://developers.google.com/youtube/v3/docs/channels/update). Title is
# deliberately *not* an accepted override anywhere in this module (see
# update_channel_branding's docstring) -- channels.update rejects any
# actual title change outright.

def get_own_channel_branding(youtube, *, retry_budget: RetryBudget | None = None, deadline=None) -> dict:
    resp = _execute(
        youtube.channels().list(part="brandingSettings", mine=True), retry_budget=retry_budget, deadline=deadline,
    )
    items = resp.get("items", [])
    if not items:
        raise ApiError("no channel found for the authenticated account (channels.list mine=true)")
    return items[0].get("brandingSettings", {})


def merge_channel_branding(current: dict, *, description: str) -> dict:
    """``current``: the whole ``brandingSettings`` object (not just its
    ``channel`` sub-object) -- copied through as-is so any other
    sub-object the API still returns (e.g. legacy ``hints``) survives
    the round trip untouched, same "never drop what the caller didn't
    touch" rule as ``merge_playlist_snippet``/``merge_snippet``.

    ``description`` is the only mutable field this function (or its only
    caller, ``update_channel_branding``) ever accepts -- deliberately no
    ``title`` parameter at all: ``channels.update`` rejects any actual
    title change (``channelTitleUpdateForbidden``), so there is no safe
    way to expose one here. ``update_channel_branding`` re-sends the
    channel's current title unconditionally (not via a caller-suppliable
    override), so this can never be bypassed by a future call site
    accidentally passing a differing title through."""
    merged = dict(current)
    channel = dict(merged.get("channel", {}))
    channel["description"] = description
    merged["channel"] = channel
    return merged


def update_channel_branding(
    youtube, channel_id: str, *, description: str, current_branding: dict | None = None,
    retry_budget: RetryBudget | None = None, deadline=None,
) -> dict:
    """``current_branding``: pass in an already-fetched ``brandingSettings``
    object to avoid a redundant second ``channels.list`` call (same
    rationale as ``update_playlist_snippet``/``update_snippet``).

    ``brandingSettings.channel.title`` must equal the channel's *current*
    title (or be omitted) -- the API rejects any actual change with
    ``channelTitleUpdateForbidden`` (renaming a channel is only possible
    via YouTube Studio, not this API). This function always re-sends the
    current, unchanged title read back from ``current_branding`` -- there
    is no parameter that can override it -- and refuses (fail-closed,
    never silently sends a null title) if the readback didn't even have
    one, rather than ever risking a title change slipping through."""
    current = current_branding if current_branding is not None else get_own_channel_branding(
        youtube, retry_budget=retry_budget, deadline=deadline,
    )
    # ``or {}`` (not just ``.get(..., {})``): an explicit ``channel: null``
    # in a malformed/unexpected API response must fail the same ApiError
    # path below, not raise an unrelated AttributeError from chaining
    # .get() onto None (Codex light review follow-up).
    current_title = (current.get("channel") or {}).get("title")
    if not current_title:
        raise ApiError(
            "channels.list (part=brandingSettings) returned no current channel.title to preserve; refusing to update"
        )
    merged = merge_channel_branding(current, description=description)
    merged["channel"]["title"] = current_title
    # channels.update is idempotent -- retry allowed.
    return _execute(
        youtube.channels().update(part="brandingSettings", body={"id": channel_id, "brandingSettings": merged}),
        retry_budget=retry_budget, deadline=deadline,
    )


def delete_video(youtube, video_id: str) -> None:
    _execute_once(youtube.videos().delete(id=video_id))


# -- meta set (M1改: read -> merge -> update, never drops fields) -------------

def merge_snippet(current: dict, *, title=None, description=None, tags=None, category_id=None) -> dict:
    merged = dict(current)
    if title is not None:
        merged["title"] = title
    if description is not None:
        merged["description"] = description
    if tags is not None:
        merged["tags"] = tags
    if category_id is not None:
        merged["categoryId"] = category_id
    return merged


def update_snippet(
    youtube, video_id: str, *, current_snippet: dict | None = None,
    retry_budget: RetryBudget | None = None, deadline=None, **overrides
) -> dict:
    """``current_snippet``: pass in an already-fetched snippet (e.g. one
    the caller needed anyway to derive an override) to avoid a redundant
    second ``videos.list`` call and the TOCTOU window between the two
    reads (Codex review, minor #14)."""
    current = current_snippet if current_snippet is not None else get_video_snippet(
        youtube, video_id, retry_budget=retry_budget, deadline=deadline,
    )
    merged = merge_snippet(current, **overrides)
    # title + categoryId must always be re-sent (part=snippet replaces the whole part)
    merged.setdefault("title", current.get("title"))
    merged.setdefault("categoryId", current.get("categoryId"))
    # videos.update is idempotent -- retry allowed.
    return _execute(
        youtube.videos().update(part="snippet", body={"id": video_id, "snippet": merged}),
        retry_budget=retry_budget, deadline=deadline,
    )
