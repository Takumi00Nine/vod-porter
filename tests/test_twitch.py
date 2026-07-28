"""M13/M14 AC: cursor pagination + early cutoff by range start, single
/users lookup reused, 401 triggers exactly one re-auth (no infinite
recursion), all calls carry timeout, duration parsing is fullmatch-based
(no fail-open on unparseable input)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.fakes import FakeResponse, FakeSession
from vodp.core.errors import ApiError
from vodp.infra.twitch import TwitchClient, parse_twitch_duration


def _video(vid, created_at, duration="1h0m0s"):
    return {
        "id": vid, "title": f"title-{vid}", "created_at": created_at,
        "duration": duration, "url": f"https://twitch.tv/videos/{vid}",
    }


def test_parse_duration_full_hms():
    assert parse_twitch_duration("2h21m23s") == 2 * 3600 + 21 * 60 + 23


def test_parse_duration_minutes_only():
    assert parse_twitch_duration("45m") == 45 * 60


def test_parse_duration_empty_is_unknown():
    assert parse_twitch_duration("") is None


def test_parse_duration_garbage_is_unknown_not_zero():
    assert parse_twitch_duration("garbage") is None


def test_get_app_token_caches(monkeypatch):
    session = FakeSession()
    session.queue("oauth2/token", FakeResponse(200, {"access_token": "tok1"}))
    client = TwitchClient("cid", "csecret", session)
    assert client.get_app_token() == "tok1"
    assert client.get_app_token() == "tok1"
    token_calls = [c for c in session.calls if "oauth2/token" in c["url"]]
    assert len(token_calls) == 1  # cached, no second call


def test_get_user_id_caches_and_reuses(monkeypatch):
    session = FakeSession()
    session.queue("oauth2/token", FakeResponse(200, {"access_token": "tok1"}))
    session.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    client = TwitchClient("cid", "csecret", session)
    assert client.get_user_id("someuser") == "999"
    assert client.get_user_id("someuser") == "999"
    user_calls = [c for c in session.calls if "/users" in c["url"]]
    assert len(user_calls) == 1


def test_401_triggers_single_reauth_then_succeeds():
    session = FakeSession()
    session.queue("oauth2/token", FakeResponse(200, {"access_token": "tok1"}))
    session.queue("oauth2/token", FakeResponse(200, {"access_token": "tok2"}))
    session.queue("/users", FakeResponse(401, {"error": "unauthorized"}))
    session.queue("/users", FakeResponse(200, {"data": [{"id": "123"}]}))
    client = TwitchClient("cid", "csecret", session)
    assert client.get_user_id("chan") == "123"


def test_401_twice_raises_api_error_not_infinite_recursion():
    session = FakeSession()
    session.queue("oauth2/token", FakeResponse(200, {"access_token": "tok1"}))
    session.queue("oauth2/token", FakeResponse(200, {"access_token": "tok2"}))
    session.queue("/users", FakeResponse(401, {"error": "unauthorized"}))
    session.queue("/users", FakeResponse(401, {"error": "still unauthorized"}))
    client = TwitchClient("cid", "csecret", session)
    with pytest.raises(ApiError):
        client.get_user_id("chan")


def test_all_requests_carry_timeout():
    session = FakeSession()
    session.queue("oauth2/token", FakeResponse(200, {"access_token": "tok1"}))
    session.queue("/users", FakeResponse(200, {"data": [{"id": "1"}]}))
    client = TwitchClient("cid", "csecret", session, timeout=5.0)
    client.get_user_id("chan")
    assert all("timeout" in c and c["timeout"] for c in session.calls)


def test_list_videos_stops_at_range_start_across_two_pages():
    now = datetime(2026, 7, 27, tzinfo=timezone.utc)
    page1 = {
        "data": [
            _video("v3", (now).isoformat().replace("+00:00", "Z")),
            _video("v2", (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")),
        ],
        "pagination": {"cursor": "cur1"},
    }
    page2 = {
        "data": [
            _video("v1", (now - timedelta(days=2)).isoformat().replace("+00:00", "Z")),
            _video("v0", (now - timedelta(days=10)).isoformat().replace("+00:00", "Z")),  # out of range
        ],
        "pagination": {},
    }
    session = FakeSession()
    session.queue("oauth2/token", FakeResponse(200, {"access_token": "tok1"}))
    session.queue("/videos", FakeResponse(200, page1))
    session.queue("/videos", FakeResponse(200, page2))
    client = TwitchClient("cid", "csecret", session)

    range_start = now - timedelta(days=3)
    result = client.list_videos("999", range_start=range_start)

    assert result.ok is True
    assert result.complete is True
    ids = [v.id for v in result.items]
    assert ids == ["v3", "v2", "v1"]  # v0 excluded (older than range start)
    video_calls = [c for c in session.calls if "/videos" in c["url"]]
    assert len(video_calls) == 2
    assert video_calls[1]["params"]["after"] == "cur1"


def test_list_videos_incomplete_when_max_pages_reached_before_start():
    now = datetime(2026, 7, 27, tzinfo=timezone.utc)
    page = {"data": [_video("v1", now.isoformat().replace("+00:00", "Z"))], "pagination": {"cursor": "next"}}
    session = FakeSession()
    session.queue("oauth2/token", FakeResponse(200, {"access_token": "tok1"}))
    for _ in range(3):
        session.queue("/videos", FakeResponse(200, page))
    client = TwitchClient("cid", "csecret", session)

    result = client.list_videos("999", range_start=now - timedelta(days=365), max_pages=2)
    assert result.ok is False
    assert result.complete is False
    assert result.error is not None


def test_api_failure_returns_not_ok_distinct_from_zero_results():
    session = FakeSession()
    session.queue("oauth2/token", FakeResponse(200, {"access_token": "tok1"}))
    session.queue("/videos", FakeResponse(500, {"error": "server error"}))
    client = TwitchClient("cid", "csecret", session)
    result = client.list_videos("999", range_start=datetime.now(timezone.utc))
    assert result.ok is False
    assert result.items == []


# -- M15: retry budget wiring (idempotent GET only) --------------------------

def test_retry_budget_retries_transient_5xx_then_succeeds():
    from vodp.infra.retry import RetryBudget

    session = FakeSession()
    session.queue("oauth2/token", FakeResponse(200, {"access_token": "tok1"}))
    session.queue("/users", FakeResponse(500, {"error": "boom"}))
    session.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    budget = RetryBudget(max_attempts=3, base_delay=0.0)
    client = TwitchClient("cid", "csecret", session, retry_budget=budget)

    user_id = client.get_user_id("chan")
    assert user_id == "999"
    assert budget.attempts_used == 1


def test_no_retry_budget_means_single_5xx_fails_immediately():
    session = FakeSession()
    session.queue("oauth2/token", FakeResponse(200, {"access_token": "tok1"}))
    session.queue("/users", FakeResponse(500, {"error": "boom"}))
    client = TwitchClient("cid", "csecret", session)  # no retry_budget
    with pytest.raises(ApiError):
        client.get_user_id("chan")


def test_retry_budget_exhausted_raises_api_error():
    from vodp.infra.retry import RetryBudget

    session = FakeSession()
    session.queue("oauth2/token", FakeResponse(200, {"access_token": "tok1"}))
    for _ in range(5):
        session.queue("/users", FakeResponse(503, {"error": "still down"}))
    budget = RetryBudget(max_attempts=2, base_delay=0.0)
    client = TwitchClient("cid", "csecret", session, retry_budget=budget)
    with pytest.raises(ApiError):
        client.get_user_id("chan")


def test_retry_budget_does_not_retry_non_retryable_status():
    from vodp.infra.retry import RetryBudget

    session = FakeSession()
    session.queue("oauth2/token", FakeResponse(200, {"access_token": "tok1"}))
    session.queue("/users", FakeResponse(404, {"error": "not found"}))
    budget = RetryBudget(max_attempts=5, base_delay=0.0)
    client = TwitchClient("cid", "csecret", session, retry_budget=budget)
    with pytest.raises(ApiError):
        client.get_user_id("chan")
    assert budget.attempts_used == 0  # never consumed -- 404 isn't retryable
