"""M10 (insert full snippet/private), M11 (publish readback), M12 (playlist
resolve/dedup), M1改 (videos list via uploads playlist, meta set merge)."""

from __future__ import annotations

import pytest

from tests.fakes import FakeYouTube
from vodp.core.errors import ApiError, AuthRequiredError
from vodp.infra import youtube as yt


# -- M10 insert -----------------------------------------------------------

def test_insert_video_sends_full_snippet_and_private_status():
    fake = FakeYouTube()
    fake.queue("videos", "insert", {"id": "vid123"})
    video_id = yt.insert_video(
        fake, media_body=object(), title="T", description="D", tags=["a", "b"], category_id="22",
    )
    assert video_id == "vid123"
    call = fake.calls[0]
    assert call["kwargs"]["part"] == "snippet,status"
    body = call["kwargs"]["body"]
    assert body["snippet"] == {"title": "T", "description": "D", "tags": ["a", "b"], "categoryId": "22"}
    assert body["status"]["privacyStatus"] == "private"
    assert body["status"]["selfDeclaredMadeForKids"] is False


# -- M11 publish + verify --------------------------------------------------

def test_publish_video_update_part_is_status_only():
    fake = FakeYouTube()
    fake.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private", "embeddable": True}}]})
    fake.queue("videos", "update", {"id": "v1"})
    yt.publish_video(fake, "v1", "public")
    update_call = [c for c in fake.calls if c["method"] == "update"][0]
    assert update_call["kwargs"]["part"] == "status"
    assert update_call["kwargs"]["body"]["status"]["privacyStatus"] == "public"
    assert update_call["kwargs"]["body"]["status"]["embeddable"] is True  # preserved allowlist field


def test_verify_privacy_succeeds_immediately_when_matching():
    fake = FakeYouTube()
    fake.queue("videos", "list", {"items": [{"status": {"privacyStatus": "public"}}]})
    assert yt.verify_privacy(fake, "v1", "public", sleep=lambda s: None) is True


def test_verify_privacy_fails_after_retries_when_still_private():
    fake = FakeYouTube()
    for _ in range(5):
        fake.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})
    sleeps = []
    assert yt.verify_privacy(fake, "v1", "public", attempts=5, sleep=sleeps.append) is False
    assert len(sleeps) == 4  # sleeps between attempts, not after the last


# -- videos list (uploads playlist, no search.list) ------------------------

def test_list_my_uploaded_videos_uses_uploads_playlist_with_pagination():
    fake = FakeYouTube()
    fake.queue("channels", "list", {
        "items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UUxyz"}}}]
    })
    fake.queue("playlistItems", "list", {
        "items": [{"contentDetails": {"videoId": "v1"}}],
        "nextPageToken": "page2",
    })
    fake.queue("playlistItems", "list", {
        "items": [{"contentDetails": {"videoId": "v2"}}],
    })
    items = yt.list_my_uploaded_videos(fake)
    assert [i["contentDetails"]["videoId"] for i in items] == ["v1", "v2"]
    channel_calls = [c for c in fake.calls if c["resource"] == "channels"]
    assert channel_calls[0]["kwargs"]["mine"] is True
    playlist_item_calls = [c for c in fake.calls if c["resource"] == "playlistItems"]
    assert playlist_item_calls[1]["kwargs"]["pageToken"] == "page2"


# -- M12 playlist resolve/dedup --------------------------------------------

def test_find_playlist_by_name_exact_match():
    fake = FakeYouTube()
    fake.queue("playlists", "list", {
        "items": [
            {"id": "PL1", "snippet": {"title": "Archive"}},
            {"id": "PL2", "snippet": {"title": "Other"}},
        ]
    })
    found = yt.find_playlist_by_name(fake, "Archive")
    assert found["id"] == "PL1"


def test_find_playlist_by_name_none_when_absent():
    fake = FakeYouTube()
    fake.queue("playlists", "list", {"items": []})
    assert yt.find_playlist_by_name(fake, "Nope") is None


def test_find_playlist_by_name_raises_on_multiple_matches():
    fake = FakeYouTube()
    fake.queue("playlists", "list", {
        "items": [
            {"id": "PL1", "snippet": {"title": "Archive"}},
            {"id": "PL2", "snippet": {"title": "Archive"}},
        ]
    })
    with pytest.raises(ApiError):
        yt.find_playlist_by_name(fake, "Archive")


def test_create_playlist_body():
    fake = FakeYouTube()
    fake.queue("playlists", "insert", {"id": "PL9"})
    result = yt.create_playlist(fake, "New List", "public")
    assert result["id"] == "PL9"
    call = fake.calls[0]
    assert call["kwargs"]["body"]["snippet"]["title"] == "New List"
    assert call["kwargs"]["body"]["status"]["privacyStatus"] == "public"


def test_playlist_contains_video_true_and_false():
    fake = FakeYouTube()
    fake.queue("playlistItems", "list", {"items": [{"contentDetails": {"videoId": "v1"}}]})
    assert yt.playlist_contains_video(fake, "PL1", "v1") is True

    fake2 = FakeYouTube()
    fake2.queue("playlistItems", "list", {"items": [{"contentDetails": {"videoId": "vOther"}}]})
    assert yt.playlist_contains_video(fake2, "PL1", "v1") is False


def test_add_video_to_playlist_body():
    fake = FakeYouTube()
    fake.queue("playlistItems", "insert", {"id": "PLI1"})
    yt.add_video_to_playlist(fake, "PL1", "v1")
    call = fake.calls[0]
    assert call["kwargs"]["body"]["snippet"]["playlistId"] == "PL1"
    assert call["kwargs"]["body"]["snippet"]["resourceId"]["videoId"] == "v1"


# -- meta set: read-merge-update, never drops fields ------------------------

def test_update_snippet_preserves_unspecified_fields():
    fake = FakeYouTube()
    fake.queue("videos", "list", {
        "items": [{"snippet": {"title": "Old", "description": "OldDesc", "tags": ["x"], "categoryId": "20"}}]
    })
    fake.queue("videos", "update", {"id": "v1"})
    yt.update_snippet(fake, "v1", title="New Title")
    update_call = [c for c in fake.calls if c["method"] == "update"][0]
    snippet = update_call["kwargs"]["body"]["snippet"]
    assert snippet["title"] == "New Title"
    assert snippet["description"] == "OldDesc"  # preserved, not dropped
    assert snippet["tags"] == ["x"]
    assert snippet["categoryId"] == "20"  # required re-send, preserved


def test_update_snippet_missing_video_raises_api_error():
    fake = FakeYouTube()
    fake.queue("videos", "list", {"items": []})
    with pytest.raises(ApiError):
        yt.update_snippet(fake, "missing", title="x")


# -- channel update: merge -> update, title never overridable ----------------

def test_update_channel_branding_preserves_unspecified_fields_and_current_title():
    fake = FakeYouTube()
    fake.queue("channels", "list", {"items": [{"brandingSettings": {
        "channel": {"title": "My Channel", "description": "old", "keywords": "a b"},
        "hints": [{"property": "x", "value": "y"}],
    }}]})
    fake.queue("channels", "update", {"id": "UCabc"})
    yt.update_channel_branding(fake, "UCabc", description="new")
    update_call = [c for c in fake.calls if c["resource"] == "channels" and c["method"] == "update"][0]
    body = update_call["kwargs"]["body"]
    assert body["id"] == "UCabc"
    branding = body["brandingSettings"]
    assert branding["channel"]["title"] == "My Channel"  # re-sent unchanged, never overridable
    assert branding["channel"]["description"] == "new"
    assert branding["channel"]["keywords"] == "a b"  # untouched field preserved
    assert branding["hints"] == [{"property": "x", "value": "y"}]  # untouched sub-object preserved


def test_update_channel_branding_missing_title_raises_api_error_fail_closed():
    fake = FakeYouTube()
    fake.queue("channels", "list", {"items": [{"brandingSettings": {"channel": {"description": "old"}}}]})
    with pytest.raises(ApiError):
        yt.update_channel_branding(fake, "UCabc", description="new")
    assert not any(c["method"] == "update" for c in fake.calls)


def test_update_channel_branding_null_channel_object_raises_api_error_not_attribute_error():
    """Codex light review follow-up: an explicit ``channel: null`` in a
    malformed API response must fail the same ApiError path, not raise
    an unrelated AttributeError from chaining .get() onto None."""
    fake = FakeYouTube()
    fake.queue("channels", "list", {"items": [{"brandingSettings": {"channel": None}}]})
    with pytest.raises(ApiError):
        yt.update_channel_branding(fake, "UCabc", description="new")
    assert not any(c["method"] == "update" for c in fake.calls)


# -- M15: retry_budget wiring (idempotent GET/update only) -------------------

def test_get_video_status_retries_with_budget_on_5xx(monkeypatch):
    from googleapiclient.errors import HttpError
    from vodp.infra.retry import RetryBudget

    fake = FakeYouTube()
    http_error = HttpError(resp=type("R", (), {"status": 500, "reason": "Server Error"})(), content=b"boom")
    fake.queue("videos", "list", http_error)
    fake.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})
    budget = RetryBudget(max_attempts=3, base_delay=0.0)
    monkeypatch.setattr("time.sleep", lambda s: None)

    status = yt.get_video_status(fake, "v1", retry_budget=budget)
    assert status["privacyStatus"] == "private"
    assert budget.attempts_used == 1


def test_get_video_status_without_budget_raises_api_error_immediately():
    """Codex review, major #8: a surviving HttpError is always converted
    to ApiError (with or without a retry budget), so per-video isolation
    upstream (which only catches VodpError/ApiError) works consistently."""
    from googleapiclient.errors import HttpError

    fake = FakeYouTube()
    http_error = HttpError(resp=type("R", (), {"status": 500, "reason": "Server Error"})(), content=b"boom")
    fake.queue("videos", "list", http_error)
    with pytest.raises(ApiError):
        yt.get_video_status(fake, "v1")


def test_get_video_status_401_raises_auth_required_not_api_error():
    """R8/M6 state I (Codex/tester review round 2): a live 401 encountered
    *during use* (credentials looked valid at authenticate() time, then
    got rejected mid-run -- e.g. the user revoked vodp's access) must map
    to AuthRequiredError (exit 4, "run `vodp auth login` again"), never
    fall through to the generic ApiError (5) every other HTTP status
    gets."""
    from googleapiclient.errors import HttpError

    fake = FakeYouTube()
    http_error = HttpError(resp=type("R", (), {"status": 401, "reason": "Unauthorized"})(), content=b"invalid_grant")
    fake.queue("videos", "list", http_error)
    with pytest.raises(AuthRequiredError):
        yt.get_video_status(fake, "v1")


def test_get_video_status_401_with_retry_budget_still_raises_auth_required_not_retried():
    """A 401 is not in the retryable-status set at all (unlike 429/5xx) --
    confirm this holds true with a retry_budget supplied too, and that no
    retry attempt is consumed trying to work around it."""
    from googleapiclient.errors import HttpError
    from vodp.infra.retry import RetryBudget

    fake = FakeYouTube()
    http_error = HttpError(resp=type("R", (), {"status": 401, "reason": "Unauthorized"})(), content=b"invalid_grant")
    fake.queue("videos", "list", http_error)
    budget = RetryBudget(max_attempts=3, base_delay=0.0)
    with pytest.raises(AuthRequiredError):
        yt.get_video_status(fake, "v1", retry_budget=budget)
    assert budget.attempts_used == 0


def test_get_video_status_budget_exhausted_raises_api_error_not_http_error(monkeypatch):
    from googleapiclient.errors import HttpError
    from vodp.infra.retry import RetryBudget

    fake = FakeYouTube()
    http_error = HttpError(resp=type("R", (), {"status": 503, "reason": "Unavailable"})(), content=b"boom")
    for _ in range(5):
        fake.queue("videos", "list", http_error)
    budget = RetryBudget(max_attempts=2, base_delay=0.0)
    monkeypatch.setattr("time.sleep", lambda s: None)
    with pytest.raises(ApiError):
        yt.get_video_status(fake, "v1", retry_budget=budget)


def test_insert_video_never_accepts_retry_budget_param():
    """M15/§4: videos.insert is one of the three non-idempotent calls --
    it must not even have a retry_budget parameter to pass."""
    import inspect

    assert "retry_budget" not in inspect.signature(yt.insert_video).parameters
    assert "retry_budget" not in inspect.signature(yt.create_playlist).parameters
    assert "retry_budget" not in inspect.signature(yt.add_video_to_playlist).parameters


def test_insert_video_calls_next_chunk_with_num_retries_zero():
    fake = FakeYouTube()
    recorded = {}

    class RecordingRequest:
        def next_chunk(self, num_retries=None):
            recorded["num_retries"] = num_retries
            return None, {"id": "v1"}

    fake.videos = lambda: type("R", (), {"insert": lambda *a, **kw: RecordingRequest()})()
    yt.insert_video(fake, media_body=object(), title="t", description="d", tags=[], category_id="22")
    assert recorded["num_retries"] == 0


def test_insert_video_checks_deadline_before_each_chunk(monkeypatch):
    """M15 (partial propagation, Codex review major #7): insert_video
    checks the deadline before every next_chunk() call."""
    from vodp.infra.deadline import Deadline, DeadlineExceededError

    clock = {"t": 0.0}
    monkeypatch.setattr("time.monotonic", lambda: clock["t"])
    deadline = Deadline(total_seconds=10.0)

    class SlowRequest:
        def __init__(self):
            self.calls = 0

        def next_chunk(self, num_retries=None):
            self.calls += 1
            clock["t"] += 20.0  # blow the deadline mid-upload
            return object(), None  # not done yet -- the *next* chunk must never start

    fake = FakeYouTube()
    request = SlowRequest()
    fake.videos = lambda: type("R", (), {"insert": lambda *a, **kw: request})()

    with pytest.raises(DeadlineExceededError):
        yt.insert_video(
            fake, media_body=object(), title="t", description="d", tags=[], category_id="22", deadline=deadline,
        )
    assert request.calls == 1  # first chunk ran, but the *next* one was never started


def test_insert_video_401_mid_chunk_raises_auth_required_not_raw_http_error():
    """Codex light review (round-2 follow-up on R8): insert_video's
    next_chunk() loop doesn't go through _execute() at all -- a 401
    there used to leak as a raw, unconverted HttpError instead of the
    same AuthRequiredError every other call site gets."""
    from googleapiclient.errors import HttpError

    fake = FakeYouTube()
    http_error = HttpError(resp=type("R", (), {"status": 401, "reason": "Unauthorized"})(), content=b"invalid_grant")

    class UnauthorizedRequest:
        def next_chunk(self, num_retries=None):
            raise http_error

    fake.videos = lambda: type("R", (), {"insert": lambda *a, **kw: UnauthorizedRequest()})()
    with pytest.raises(AuthRequiredError):
        yt.insert_video(fake, media_body=object(), title="t", description="d", tags=[], category_id="22")


def test_delete_video_401_raises_auth_required_not_raw_http_error():
    """Same conversion gap as insert_video's next_chunk(), for the
    delete_* helpers -- none of them go through _execute()/retry_budget
    at all (deleting twice isn't safely repeatable either)."""
    from googleapiclient.errors import HttpError

    fake = FakeYouTube()
    http_error = HttpError(resp=type("R", (), {"status": 401, "reason": "Unauthorized"})(), content=b"invalid_grant")
    fake.queue("videos", "delete", http_error)
    with pytest.raises(AuthRequiredError):
        yt.delete_video(fake, "v1")


def test_create_playlist_401_raises_auth_required_not_raw_http_error():
    from googleapiclient.errors import HttpError

    fake = FakeYouTube()
    http_error = HttpError(resp=type("R", (), {"status": 401, "reason": "Unauthorized"})(), content=b"invalid_grant")
    fake.queue("playlists", "insert", http_error)
    with pytest.raises(AuthRequiredError):
        yt.create_playlist(fake, "Archive", "public")


def test_get_video_status_refresh_error_raises_auth_required_not_leaked():
    """F3 (Codex/tester review round 2, differed-items follow-up): a 401
    ``HttpError`` isn't the only way state I (design.md §5) shows up --
    googleapiclient's transport can also raise a bare
    ``google.auth.exceptions.RefreshError`` directly (not wrapped in an
    HttpError at all) when it tries to silently refresh an expired
    access token mid-call and the refresh itself is rejected
    (invalid_grant/revoked refresh_token). This must get the exact same
    AuthRequiredError(4) treatment, not leak past _execute() entirely."""
    from google.auth.exceptions import RefreshError

    fake = FakeYouTube()
    fake.queue("videos", "list", RefreshError("invalid_grant: Token has been expired or revoked."))
    with pytest.raises(AuthRequiredError):
        yt.get_video_status(fake, "v1")


def test_get_video_status_refresh_error_with_retry_budget_not_retried():
    from google.auth.exceptions import RefreshError
    from vodp.infra.retry import RetryBudget

    fake = FakeYouTube()
    fake.queue("videos", "list", RefreshError("invalid_grant"))
    budget = RetryBudget(max_attempts=3, base_delay=0.0)
    with pytest.raises(AuthRequiredError):
        yt.get_video_status(fake, "v1", retry_budget=budget)
    assert budget.attempts_used == 0


def test_insert_video_refresh_error_mid_chunk_raises_auth_required():
    """Same RefreshError conversion for insert_video's next_chunk() loop
    -- it doesn't go through _execute() at all."""
    from google.auth.exceptions import RefreshError

    fake = FakeYouTube()

    class RefreshErrorRequest:
        def next_chunk(self, num_retries=None):
            raise RefreshError("invalid_grant")

    fake.videos = lambda: type("R", (), {"insert": lambda *a, **kw: RefreshErrorRequest()})()
    with pytest.raises(AuthRequiredError):
        yt.insert_video(fake, media_body=object(), title="t", description="d", tags=[], category_id="22")


def test_delete_video_refresh_error_raises_auth_required():
    from google.auth.exceptions import RefreshError

    fake = FakeYouTube()
    fake.queue("videos", "delete", RefreshError("invalid_grant"))
    with pytest.raises(AuthRequiredError):
        yt.delete_video(fake, "v1")
