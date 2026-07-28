"""``vodp vods list`` end-to-end: argv -> exit code -> JSON (M1/M13)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import vodp.cli.commands.vods as vods_module
from tests.fakes import FakeResponse, FakeSession
from vodp.cli.main import main
from vodp.core import exitcodes


def _video(vid, created_at_iso):
    return {
        "id": vid, "title": f"t-{vid}", "created_at": created_at_iso,
        "duration": "1h0m0s", "url": f"https://twitch.tv/videos/{vid}",
    }


@pytest.fixture
def fake_session(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(vods_module.requests, "Session", lambda: session)
    return session


def _write_env(home):
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text(
        "TWITCH_CLIENT_ID=cid\nTWITCH_CLIENT_SECRET=csecret\nTWITCH_CHANNEL_LOGIN=chan\n",
        encoding="utf-8",
    )


def test_vods_list_returns_videos_in_range(vodp_home, fake_session, capsys):
    _write_env(vodp_home)
    fake_session.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_session.queue("/users", FakeResponse(200, {"data": [{"id": "1"}]}))
    fake_session.queue("/videos", FakeResponse(200, {
        "data": [_video("v1", "2026-07-01T10:00:00Z")],
        "pagination": {},
    }))

    code = main(["vods", "list", "--home", str(vodp_home), "--range", "2026-07-01", "2026-07-01"])
    out = json.loads(capsys.readouterr().out)
    assert code == exitcodes.OK
    assert out["videos"][0]["twitch_vod_id"] == "v1"
    assert out["summary"]["total"] == 1


def test_vods_list_zero_results_exits_3(vodp_home, fake_session, capsys):
    _write_env(vodp_home)
    fake_session.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_session.queue("/users", FakeResponse(200, {"data": [{"id": "1"}]}))
    fake_session.queue("/videos", FakeResponse(200, {"data": [], "pagination": {}}))

    code = main(["vods", "list", "--home", str(vodp_home), "--range", "2026-07-01", "2026-07-01"])
    assert code == exitcodes.NO_TARGETS


def test_vods_list_missing_env_config_exits_5(vodp_home, capsys):
    code = main(["vods", "list", "--home", str(vodp_home), "--range", "2026-07-01", "2026-07-01"])
    assert code == exitcodes.ENV_OR_API_ERROR


def test_vods_list_api_failure_exits_5_not_3(vodp_home, fake_session, capsys):
    _write_env(vodp_home)
    fake_session.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_session.queue("/users", FakeResponse(200, {"data": [{"id": "1"}]}))
    fake_session.queue("/videos", FakeResponse(500, {"error": "boom"}))

    code = main(["vods", "list", "--home", str(vodp_home), "--range", "2026-07-01", "2026-07-01"])
    assert code == exitcodes.ENV_OR_API_ERROR


def test_vods_list_excludes_videos_outside_range(vodp_home, fake_session, capsys):
    _write_env(vodp_home)
    fake_session.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_session.queue("/users", FakeResponse(200, {"data": [{"id": "1"}]}))
    fake_session.queue("/videos", FakeResponse(200, {
        "data": [
            _video("v_in", "2026-07-01T12:00:00Z"),
            _video("v_out_future", "2026-07-03T00:00:00Z"),
        ],
        "pagination": {},
    }))

    code = main(["vods", "list", "--home", str(vodp_home), "--range", "2026-07-01", "2026-07-01"])
    out = json.loads(capsys.readouterr().out)
    ids = [v["twitch_vod_id"] for v in out["videos"]]
    assert ids == ["v_in"]
