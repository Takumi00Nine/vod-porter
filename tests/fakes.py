"""Fake network/subprocess doubles shared across tests (design.md §10).

Nothing here ever opens a real socket or subprocess; conftest.block_network
enforces that at the socket layer as a backstop.
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass, field


@dataclass
class FakeResponse:
    status_code: int
    _json: dict | list
    text: str = ""

    def json(self):
        return self._json

    def __post_init__(self):
        if not self.text:
            self.text = _json.dumps(self._json)


class FakeSession:
    """Records every call it receives and replays canned responses in
    order per (method, url-substring) key, or via a callable handler."""

    def __init__(self):
        self.calls: list[dict] = []
        self._handlers: dict[str, list] = {}

    def queue(self, key: str, response_or_fn) -> None:
        self._handlers.setdefault(key, []).append(response_or_fn)

    def _dispatch(self, method: str, url: str, **kwargs) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        assert "timeout" in kwargs, f"missing timeout on {method} {url}"
        for key, handlers in self._handlers.items():
            if key in url:
                handler = handlers.pop(0) if len(handlers) > 1 else handlers[0]
                if callable(handler) and not isinstance(handler, FakeResponse):
                    return handler(**kwargs)
                return handler
        raise AssertionError(f"no fake handler queued for {method} {url}")

    def get(self, url, **kwargs):
        return self._dispatch("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._dispatch("POST", url, **kwargs)


class FakeYouTubeRequest:
    """Stands in for a googleapiclient ``HttpRequest``: ``.execute()`` for
    plain calls, ``.next_chunk()`` for the resumable upload path."""

    def __init__(self, youtube: "FakeYouTube", resource: str, method: str, kwargs: dict):
        self.youtube = youtube
        self.resource = resource
        self.method = method
        self.kwargs = kwargs

    def execute(self, num_retries=None):
        self.youtube.calls.append({"resource": self.resource, "method": self.method, "kwargs": self.kwargs})
        key = (self.resource, self.method)
        queue = self.youtube.responses.get(key)
        if not queue:
            raise AssertionError(f"no fake YouTube response queued for {key} (kwargs={self.kwargs})")
        response = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(response, Exception):
            raise response
        if callable(response) and not isinstance(response, dict):
            return response(self.kwargs)
        return response

    def next_chunk(self, num_retries=None):
        return None, self.execute()


class _FakeYouTubeResource:
    def __init__(self, youtube: "FakeYouTube", name: str):
        self._youtube = youtube
        self._name = name

    def __getattr__(self, method_name):
        def _call(**kwargs):
            return FakeYouTubeRequest(self._youtube, self._name, method_name, kwargs)
        return _call


class FakeYouTube:
    """Records every (resource, method, kwargs) call and replays canned
    responses queued via .queue(resource, method, response)."""

    def __init__(self):
        self.calls: list[dict] = []
        self.responses: dict[tuple[str, str], list] = {}

    def queue(self, resource: str, method: str, response) -> None:
        self.responses.setdefault((resource, method), []).append(response)

    def videos(self):
        return _FakeYouTubeResource(self, "videos")

    def playlists(self):
        return _FakeYouTubeResource(self, "playlists")

    def playlistItems(self):
        return _FakeYouTubeResource(self, "playlistItems")

    def channels(self):
        return _FakeYouTubeResource(self, "channels")


class FakeYDL:
    """Stands in for yt_dlp.YoutubeDL: writes a small real file at the
    resolved outtmpl path and fires the 'finished' progress hook, exactly
    like the real downloader.download_vod() expects (design.md §10)."""

    content = b"fake video bytes"

    def __init__(self, opts: dict):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url: str, download: bool = True):
        outtmpl = self.opts["outtmpl"]
        path = outtmpl.replace("%(ext)s", "mp4")
        from pathlib import Path

        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(self.content)
        for hook in self.opts.get("progress_hooks", []):
            # A "downloading" callback before "finished" -- matches real
            # yt-dlp's periodic-progress behavior closely enough for F6's
            # deadline-check-in-hook AC (Codex/tester review round 2,
            # differed-items follow-up) to be testable: a hook that
            # raises on "downloading" interrupts *before* the file is
            # ever considered done, exactly like a real mid-transfer
            # deadline expiry would.
            hook({"status": "downloading", "filename": path})
            hook({"status": "finished", "filename": path})
        return {"requested_downloads": [{"filepath": path}], "duration": 60}
