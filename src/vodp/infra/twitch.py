"""Twitch Helix client (M13/M14).

Everything Twitch-specific and easy to get wrong from the legacy app/ is
concentrated here: a single app-token per run (cached, one retry on 401,
never an unbounded recursive retry), a single ``/users`` lookup per login
(cached), ``/videos`` walked with ``sort=time&after=<cursor>`` and stopped
as soon as we see a VOD older than the requested range start -- not by
requesting a fixed number of "days back" and re-fetching, unlike G9/G10.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from vodp.core.errors import ApiError
from vodp.infra.retry import RetryBudget, call_with_retry, is_retryable_status

BASE_URL = "https://api.twitch.tv/helix"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"

_DURATION_RE = re.compile(r"(?:(?P<h>\d+)h)?(?:(?P<m>\d+)m)?(?:(?P<s>\d+)s)?")


def parse_twitch_duration(duration_str: str) -> int | None:
    """'2h21m23s' -> seconds. M14: fullmatch, and an all-empty match (e.g.
    "" or garbage that matches zero groups) is duration_unknown, not 0 --
    fail-open is explicitly banned (G12)."""
    if not duration_str:
        return None
    m = _DURATION_RE.fullmatch(duration_str)
    if not m or not any(g is not None for g in m.groups()):
        return None
    hours = int(m.group("h") or 0)
    minutes = int(m.group("m") or 0)
    seconds = int(m.group("s") or 0)
    return hours * 3600 + minutes * 60 + seconds


@dataclass
class TwitchVideo:
    id: str
    title: str
    created_at: datetime  # tz-aware, UTC
    duration_seconds: int | None
    url: str
    raw_duration: str = ""


@dataclass
class FetchResult:
    ok: bool
    items: list[TwitchVideo] = field(default_factory=list)
    complete: bool = True
    error: str | None = None


def _to_utc(iso_z: str) -> datetime:
    return datetime.fromisoformat(iso_z.replace("Z", "+00:00")).astimezone(timezone.utc)


class _RetryableHTTPError(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"retryable HTTP status {status_code}")
        self.status_code = status_code


class TwitchClient:
    def __init__(
        self, client_id: str, client_secret: str, session, *, timeout: float = 10.0, deadline=None,
        retry_budget: RetryBudget | None = None,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.session = session
        self.timeout = timeout
        self.deadline = deadline
        self.retry_budget = retry_budget
        self._token: str | None = None
        self._user_id_cache: dict[str, str] = {}

    @property
    def token(self) -> str | None:
        """The cached Twitch app access token, if one has been fetched
        yet -- exposed read-only so callers (``cli/commands/vods.py``'s
        ``run_upload``) can register it with the run's secret-masking
        (F4, Codex/tester review round 2, differed-items follow-up: M19
        previously only masked the Twitch *client secret*, never the
        bearer access token minted from it, which is just as sensitive)."""
        return self._token

    def _call_timeout(self) -> float:
        if self.deadline is not None:
            return self.deadline.timeout_for(cap=self.timeout)
        return self.timeout

    def get_app_token(self, *, force: bool = False) -> str:
        if self._token is not None and not force:
            return self._token
        resp = self.session.post(
            TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            },
            timeout=self._call_timeout(),
        )
        if resp.status_code != 200:
            raise ApiError(f"twitch token request failed: {resp.status_code} {resp.text[:200]}")
        self._token = resp.json()["access_token"]
        return self._token

    def _authed_get(self, path: str, params: dict, *, _retried: bool = False) -> dict:
        """Idempotent GET (M15): retried a finite number of times on
        429/5xx if a shared ``retry_budget`` was given; the 401-then-
        refresh-then-retry-once path below is separate and unrelated to
        that budget (a fresh token always deserves exactly one fresh try)."""
        token = self.get_app_token()
        headers = {"Client-ID": self.client_id, "Authorization": f"Bearer {token}"}

        def _raw_call():
            return self.session.get(f"{BASE_URL}{path}", headers=headers, params=params, timeout=self._call_timeout())

        if self.retry_budget is not None:
            def _call_or_raise():
                resp = _raw_call()
                if is_retryable_status(resp.status_code):
                    raise _RetryableHTTPError(resp.status_code)
                return resp

            try:
                resp = call_with_retry(
                    _call_or_raise, budget=self.retry_budget,
                    is_retryable=lambda e: isinstance(e, _RetryableHTTPError),
                    deadline=self.deadline,
                )
            except _RetryableHTTPError as e:
                raise ApiError(f"twitch API {path} failed after retries: HTTP {e.status_code}") from e
        else:
            resp = _raw_call()

        if resp.status_code == 401 and not _retried:
            self.get_app_token(force=True)
            return self._authed_get(path, params, _retried=True)
        if resp.status_code != 200:
            raise ApiError(f"twitch API {path} failed: {resp.status_code} {resp.text[:200]}")
        return resp.json()

    def get_user_id(self, login: str) -> str:
        if login in self._user_id_cache:
            return self._user_id_cache[login]
        data = self._authed_get("/users", {"login": login})
        items = data.get("data", [])
        if not items:
            raise ApiError(f"twitch channel not found: {login!r}")
        user_id = items[0]["id"]
        self._user_id_cache[login] = user_id
        return user_id

    def get_video_by_id(self, video_id: str) -> TwitchVideo | None:
        """Single-VOD lookup by id -- used only to re-derive metadata for a
        ledger row that was reserved but crashed before any download/insert
        was attempted (a narrow, bounded resume-time lookup; NOT the
        per-item ``/videos?id=`` anti-pattern the discovery loop bans, see
        G10 in requirements.md)."""
        data = self._authed_get("/videos", {"id": video_id})
        items = data.get("data", [])
        if not items:
            return None
        raw = items[0]
        return TwitchVideo(
            id=raw["id"],
            title=raw["title"],
            created_at=_to_utc(raw["created_at"]),
            duration_seconds=parse_twitch_duration(raw.get("duration", "")),
            url=raw.get("url", f"https://www.twitch.tv/videos/{raw['id']}"),
            raw_duration=raw.get("duration", ""),
        )

    def list_videos(self, user_id: str, *, range_start: datetime, max_pages: int = 50) -> FetchResult:
        """Walk /videos newest-first until a VOD older than range_start is
        seen, or max_pages pages have been consumed (in which case
        ``complete=False`` -- the caller must treat this as a fetch
        failure, not "zero videos", per M13's FetchResult contract)."""
        items: list[TwitchVideo] = []
        cursor: str | None = None
        pages = 0
        while True:
            params = {"user_id": user_id, "type": "archive", "sort": "time", "first": 100}
            if cursor:
                params["after"] = cursor
            try:
                data = self._authed_get("/videos", params)
            except ApiError as e:
                return FetchResult(ok=False, items=items, complete=False, error=str(e))
            pages += 1
            page_items = data.get("data", [])
            reached_start = False
            for raw in page_items:
                created_at = _to_utc(raw["created_at"])
                if created_at < range_start:
                    reached_start = True
                    break
                items.append(
                    TwitchVideo(
                        id=raw["id"],
                        title=raw["title"],
                        created_at=created_at,
                        duration_seconds=parse_twitch_duration(raw.get("duration", "")),
                        url=raw.get("url", f"https://www.twitch.tv/videos/{raw['id']}"),
                        raw_duration=raw.get("duration", ""),
                    )
                )
            if reached_start:
                return FetchResult(ok=True, items=items, complete=True)
            cursor = data.get("pagination", {}).get("cursor")
            if not cursor or not page_items:
                return FetchResult(ok=True, items=items, complete=True)
            if pages >= max_pages:
                return FetchResult(
                    ok=False, items=items, complete=False,
                    error=f"max_pages={max_pages} reached before range start",
                )
