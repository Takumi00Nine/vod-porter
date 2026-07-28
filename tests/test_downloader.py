"""M17 AC: outtmpl includes twitch_vod_id (no same-day-same-title
collisions), finished-hook path is authoritative, quiet/noprogress set."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fakes import FakeYDL
from vodp.core.errors import EnvironmentError_
from vodp.infra.downloader import download_vod


def test_download_vod_uses_vod_id_in_filename(tmp_path):
    path = download_vod("https://twitch.tv/videos/123", tmp_path, "123", ydl_class=FakeYDL)
    assert path.name == "vodp-123.mp4"
    assert path.is_file()


def test_download_vod_two_ids_never_collide(tmp_path):
    p1 = download_vod("https://twitch.tv/videos/1", tmp_path, "1", ydl_class=FakeYDL)
    p2 = download_vod("https://twitch.tv/videos/2", tmp_path, "2", ydl_class=FakeYDL)
    assert p1 != p2
    assert p1.is_file() and p2.is_file()


def test_download_vod_sets_quiet_and_noprogress():
    captured = {}

    class RecordingYDL(FakeYDL):
        def __init__(self, opts):
            captured.update(opts)
            super().__init__(opts)

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        download_vod("https://twitch.tv/videos/1", d, "1", ydl_class=RecordingYDL)
    assert captured["quiet"] is True
    assert captured["noprogress"] is True


def test_download_vod_deadline_expired_mid_download_raises_and_stops(tmp_path, monkeypatch):
    """F6 (Codex/tester review round 2, differed-items follow-up): the
    deadline is checked on every progress_hook callback -- an expired
    deadline must interrupt yt-dlp mid-download (before "finished" is
    ever reached), not silently let it run to completion.

    Mutation-tested (team-lead follow-up, round 3): the deadline must
    still be *unexpired* when download_vod() starts (so the earlier
    ``opts["socket_timeout"] = deadline.timeout_for(...)`` call succeeds
    without raising on its own) and only expire *during* the hook's own
    ``deadline.check()`` -- an earlier version of this test set the
    clock already-expired before calling download_vod() at all, which
    made ``timeout_for()`` raise first and never actually exercised the
    hook's check() (silently "passed" even with the hook's check()
    neutered)."""
    from vodp.infra.deadline import Deadline, DeadlineExceededError

    clock = {"t": 0.0}
    monkeypatch.setattr("time.monotonic", lambda: clock["t"])
    deadline = Deadline(total_seconds=10.0)  # NOT expired yet -- timeout_for() must succeed

    hook_calls = []

    class SlowYDL:
        """Advances the clock past the deadline *inside* extract_info,
        right before invoking the progress_hook -- isolates the hook's
        own deadline.check() as the only thing that can raise here."""

        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=True):
            path = self.opts["outtmpl"].replace("%(ext)s", "mp4")
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(b"x")
            for hook in self.opts["progress_hooks"]:
                hook_calls.append("downloading")
                clock["t"] = 20.0  # deadline expires mid-transfer, between socket_timeout calc and this hook call
                hook({"status": "downloading", "filename": path})  # must raise right here
                hook_calls.append("finished (unreachable)")
                hook({"status": "finished", "filename": path})
            return {"requested_downloads": [{"filepath": path}]}

    with pytest.raises(DeadlineExceededError):
        download_vod("https://twitch.tv/videos/1", tmp_path, "1", ydl_class=SlowYDL, deadline=deadline)
    assert hook_calls == ["downloading"]  # the hook itself raised -- "finished" was never reached


def test_download_vod_sets_socket_timeout_bounded_by_remaining_deadline(tmp_path, monkeypatch):
    """F6: yt-dlp's own socket_timeout option is set from the remaining
    deadline (capped at 30s), so a truly stalled connection can't hang
    forever even between progress_hook callbacks."""
    from vodp.infra.deadline import Deadline

    clock = {"t": 0.0}
    monkeypatch.setattr("time.monotonic", lambda: clock["t"])
    deadline = Deadline(total_seconds=10.0)
    clock["t"] = 5.0  # 5s of the 10s budget already spent elsewhere

    captured = {}

    class RecordingYDL(FakeYDL):
        def __init__(self, opts):
            captured.update(opts)
            super().__init__(opts)

    download_vod("https://twitch.tv/videos/1", tmp_path, "1", ydl_class=RecordingYDL, deadline=deadline)
    assert 0 < captured["socket_timeout"] <= 5.0


def test_download_vod_raises_environment_error_when_no_file_produced(tmp_path):
    class EmptyYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=True):
            return {"requested_downloads": []}

    with pytest.raises(EnvironmentError_):
        download_vod("https://twitch.tv/videos/1", tmp_path, "1", ydl_class=EmptyYDL)
