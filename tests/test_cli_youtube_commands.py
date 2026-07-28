"""End-to-end CLI coverage for whoami/videos/meta/playlist/auth commands.

``vodp.infra.youtube.build`` is monkeypatched to hand back a pre-seeded
FakeYouTube (design.md §10); auth uses a real valid token.json fixture so
the full authenticate() path runs for real, just without any network.
"""

from __future__ import annotations

import json
import pickle
from datetime import datetime, timedelta
from pathlib import Path

import jsonschema
import pytest
from google.oauth2.credentials import Credentials

import vodp.infra.youtube as yt_module
from tests.fakes import FakeYouTube
from vodp.cli.main import main
from vodp.core import exitcodes
from vodp.infra.auth import SCOPES, token_path

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "src" / "vodp" / "schemas"


def _validate(command_schema_name, envelope):
    """R11 (Codex/tester review round 2): 7/20 schemas had a real-output
    validation test before this round -- every command exercised in this
    file now also validates its actual JSON against schemas/<command>.json,
    not just spot-checked fields."""
    schema = json.loads((SCHEMAS_DIR / f"{command_schema_name}.json").read_text())
    jsonschema.validate(instance=envelope, schema=schema)


def _seed_valid_token(home):
    token_path(home).parent.mkdir(parents=True, exist_ok=True)
    token_path(home).write_text(json.dumps({
        "token": "tok", "refresh_token": "refresh", "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "cid", "client_secret": "csecret", "scopes": list(SCOPES),
        "expiry": (datetime.utcnow() + timedelta(hours=1)).isoformat(),
    }), encoding="utf-8")


@pytest.fixture
def fake_youtube(monkeypatch):
    fake = FakeYouTube()
    monkeypatch.setattr(yt_module, "build", lambda *a, **kw: fake)
    return fake


def _run(argv, capsys):
    """M4/M8: exit 2 never carries a JSON envelope (argparse failure or a
    post-parse ArgError, both no-JSON per design.md §3) -- every other
    exit code always does. Returns (code, parsed_json_or_None)."""
    code = main(argv)
    raw = capsys.readouterr().out
    if code == exitcodes.ARG_ERROR:
        assert raw == "", f"exit 2 must not emit stdout JSON, got: {raw!r}"
        return code, None
    return code, json.loads(raw)


# -- whoami -----------------------------------------------------------------

def test_whoami(vodp_home, fake_youtube, capsys):
    _seed_valid_token(vodp_home)
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc", "snippet": {"title": "My Channel"}}]})
    code, out = _run(["whoami", "--home", str(vodp_home), "--non-interactive"], capsys)
    assert code == exitcodes.OK
    assert out["videos"][0]["youtube_channel_id"] == "UCabc"
    _validate("whoami", out)


def test_whoami_non_interactive_no_token_exits_4(vodp_home, fake_youtube, capsys):
    from vodp.infra.auth import client_secret_path

    client_secret_path(vodp_home).parent.mkdir(parents=True, exist_ok=True)
    client_secret_path(vodp_home).write_text("{}", encoding="utf-8")
    code, out = _run(["whoami", "--home", str(vodp_home), "--non-interactive"], capsys)
    assert code == exitcodes.AUTH_REQUIRED


def test_whoami_401_during_use_exits_4_not_unhandled_exception(vodp_home, fake_youtube, capsys):
    """F2 (Codex/tester review round 2, differed-items follow-up):
    `whoami` used to call ``.execute()`` directly (not routed through
    infra/youtube.py's _execute()), so a 401 leaked as a raw,
    unconverted HttpError past main.py's VodpError handling instead of
    surfacing as AuthRequiredError(4) like every other call site."""
    from googleapiclient.errors import HttpError

    _seed_valid_token(vodp_home)
    http_error = HttpError(resp=type("R", (), {"status": 401, "reason": "Unauthorized"})(), content=b"invalid_grant")
    fake_youtube.queue("channels", "list", http_error)
    code, out = _run(["whoami", "--home", str(vodp_home), "--non-interactive"], capsys)
    assert code == exitcodes.AUTH_REQUIRED


def _seed_expected_channel_profile(home, expected_channel_id):
    """F5 (Codex/tester review round 2, differed-items follow-up,
    requirements.md 推奨S7): a profile pinning [channel] expected_channel_id."""
    (home / "profiles").mkdir(parents=True, exist_ok=True)
    (home / "profiles" / "default.toml").write_text(
        f'[channel]\nexpected_channel_id = "{expected_channel_id}"\n', encoding="utf-8",
    )


# -- videos list/publish/delete ----------------------------------------------

def test_videos_list(vodp_home, fake_youtube, capsys):
    _seed_valid_token(vodp_home)
    fake_youtube.queue("channels", "list", {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]})
    fake_youtube.queue("playlistItems", "list", {"items": [
        {"contentDetails": {"videoId": "v1"}, "snippet": {"title": "T1", "publishedAt": "2026-07-01T00:00:00Z"}},
    ]})
    code, out = _run(["videos", "list", "--home", str(vodp_home), "--non-interactive"], capsys)
    assert code == exitcodes.OK
    assert out["videos"][0]["youtube_video_id"] == "v1"
    _validate("videos_list", out)


def test_videos_publish_success(vodp_home, fake_youtube, capsys):
    _seed_valid_token(vodp_home)
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})
    fake_youtube.queue("videos", "update", {"id": "v1"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "public"}}]})
    code, out = _run(
        ["videos", "publish", "--home", str(vodp_home), "--non-interactive", "--video-id", "v1", "--privacy", "public"],
        capsys,
    )
    assert code == exitcodes.OK
    assert out["videos"][0]["status"] == "uploaded"
    _validate("videos_publish", out)


def test_videos_publish_expected_channel_id_mismatch_exits_5(vodp_home, fake_youtube, capsys):
    """F5: same guard as vods upload's, for videos publish.

    Mutation-tested (team-lead follow-up, round 3): also queues every
    fake response the happy path (test_videos_publish_success) would
    consume, so removing the channel-mismatch guard would make this run
    genuinely succeed (exit 0) instead of coincidentally erroring out on
    a missing fake response that happens to map to the same exit code."""
    _seed_valid_token(vodp_home)
    _seed_expected_channel_profile(vodp_home, "UC_EXPECTED_NOT_ACTUAL")
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})  # actually authenticated as UCabc
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})
    fake_youtube.queue("videos", "update", {"id": "v1"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "public"}}]})
    code, out = _run(
        ["videos", "publish", "--home", str(vodp_home), "--non-interactive", "--video-id", "v1", "--privacy", "public"],
        capsys,
    )
    assert code == exitcodes.ENV_OR_API_ERROR
    assert not any(c["method"] == "update" for c in fake_youtube.calls)


def test_videos_publish_privacy_lock_exits_6(vodp_home, fake_youtube, capsys, monkeypatch):
    monkeypatch.setattr(yt_module.time, "sleep", lambda s: None)  # M11 polls for real, not for 24s in CI
    _seed_valid_token(vodp_home)
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})
    fake_youtube.queue("videos", "update", {"id": "v1"})
    for _ in range(5):
        fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})
    code, out = _run(
        ["videos", "publish", "--home", str(vodp_home), "--non-interactive", "--video-id", "v1", "--privacy", "public"],
        capsys,
    )
    assert code == exitcodes.PARTIAL_FAILURE
    assert out["videos"][0]["reason_code"] == "privacy_lock"


def test_videos_publish_dry_run_makes_no_calls(vodp_home, fake_youtube, capsys):
    code, out = _run(
        ["videos", "publish", "--home", str(vodp_home), "--non-interactive", "--video-id", "v1",
         "--privacy", "public", "--dry-run"],
        capsys,
    )
    assert code == exitcodes.OK
    assert fake_youtube.calls == []
    assert not token_path(vodp_home).parent.exists()  # no auth attempted either


def test_videos_delete_default_dry_run_no_delete_call(vodp_home, fake_youtube, capsys):
    _seed_valid_token(vodp_home)
    fake_youtube.queue("videos", "list", {"items": [{"snippet": {"title": "T1"}}]})
    fake_youtube.queue("videos", "delete", {})
    code, out = _run(["videos", "delete", "--home", str(vodp_home), "--non-interactive",
                       "--video-id", "v1", "--yes"], capsys)
    # non_interactive requires --yes to even reach a decision; with --yes it executes for real
    assert code == exitcodes.OK
    assert out["videos"][0]["title"] == "T1"
    delete_calls = [c for c in fake_youtube.calls if c["method"] == "delete"]
    assert len(delete_calls) == 1
    _validate("videos_delete", out)


def test_videos_delete_expected_channel_id_mismatch_exits_5(vodp_home, fake_youtube, capsys):
    """F5 (team-lead follow-up, round 3): CLI-wiring regression coverage
    for videos delete specifically, not just relying on the shared
    verify_expected_channel()/_verify_channel_if_configured() helper
    being exercised elsewhere.

    Mutation-tested: also queues the happy path's readback+delete
    responses, so removing the guard would make this genuinely succeed
    instead of coincidentally erroring on a missing fake response."""
    _seed_valid_token(vodp_home)
    _seed_expected_channel_profile(vodp_home, "UC_EXPECTED_NOT_ACTUAL")
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})  # actually authenticated as UCabc
    fake_youtube.queue("videos", "list", {"items": [{"snippet": {"title": "T1"}}]})
    fake_youtube.queue("videos", "delete", {})
    code, out = _run(
        ["videos", "delete", "--home", str(vodp_home), "--non-interactive", "--video-id", "v1", "--yes"], capsys,
    )
    assert code == exitcodes.ENV_OR_API_ERROR
    assert not any(c["method"] == "delete" for c in fake_youtube.calls)


def test_videos_delete_non_interactive_without_yes_exits_2(vodp_home, fake_youtube, capsys):
    code, out = _run(["videos", "delete", "--home", str(vodp_home), "--non-interactive", "--video-id", "v1"], capsys)
    assert code == exitcodes.ARG_ERROR


def test_videos_delete_interactive_without_yes_is_dry_run(vodp_home, fake_youtube, capsys):
    _seed_valid_token(vodp_home)
    fake_youtube.queue("videos", "list", {"items": [{"snippet": {"title": "T1"}}]})
    code, out = _run(["videos", "delete", "--home", str(vodp_home), "--video-id", "v1"], capsys)
    assert code == exitcodes.OK
    assert out["videos"][0]["reason_code"] == "dry_run"
    assert not any(c["method"] == "delete" for c in fake_youtube.calls)


def _seed_expired_refreshable_token(home):
    token_path(home).parent.mkdir(parents=True, exist_ok=True)
    token_path(home).write_text(json.dumps({
        "token": "expired-tok", "refresh_token": "refresh", "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "cid", "client_secret": "csecret", "scopes": list(SCOPES),
        "expiry": (datetime.utcnow() - timedelta(hours=1)).isoformat(),
    }), encoding="utf-8")


def test_videos_delete_dry_run_preview_never_saves_a_refreshed_token(vodp_home, fake_youtube, capsys, monkeypatch):
    """M7 follow-up (Codex light review): the dry-run *preview* path
    (no --yes) must not persist a refreshed token to disk either -- only
    a real --yes execution is allowed the ordinary auth write behavior.
    Uses a faked, always-succeeding refresh (not blocked-network) so this
    actually distinguishes the fix from "never got far enough to save
    regardless"."""
    _seed_expired_refreshable_token(vodp_home)
    before = token_path(vodp_home).read_text(encoding="utf-8")

    def fake_refresh(self, request):
        self.token = "refreshed-tok"
        self.expiry = datetime.utcnow() + timedelta(hours=1)

    monkeypatch.setattr(Credentials, "refresh", fake_refresh)
    fake_youtube.queue("videos", "list", {"items": [{"snippet": {"title": "T1"}}]})

    code, out = _run(["videos", "delete", "--home", str(vodp_home), "--video-id", "v1"], capsys)
    assert code == exitcodes.OK
    assert out["videos"][0]["reason_code"] == "dry_run"

    after = token_path(vodp_home).read_text(encoding="utf-8")
    assert before == after
    assert '"refreshed-tok"' not in after


def test_videos_delete_readback_not_found_never_calls_delete(vodp_home, fake_youtube, capsys):
    """R5 misdeletion guard: the id+title readback (design.md §3) coming
    back empty (video already gone / typo'd id) must refuse the delete
    entirely -- even with --yes -- not silently no-op through to a
    ``videos.delete`` call for an id nobody confirmed exists."""
    _seed_valid_token(vodp_home)
    fake_youtube.queue("videos", "list", {"items": []})  # readback finds nothing
    fake_youtube.queue("videos", "delete", {})
    code, out = _run(["videos", "delete", "--home", str(vodp_home), "--non-interactive",
                       "--video-id", "v1", "--yes"], capsys)
    assert code == exitcodes.ENV_OR_API_ERROR
    assert not any(c["method"] == "delete" for c in fake_youtube.calls)


# -- meta set -----------------------------------------------------------------

def test_meta_set_preserves_unspecified_fields(vodp_home, fake_youtube, capsys):
    _seed_valid_token(vodp_home)
    fake_youtube.queue("videos", "list", {
        "items": [{"snippet": {"title": "Old", "description": "OldDesc", "tags": ["x"], "categoryId": "20"}}]
    })
    fake_youtube.queue("videos", "update", {"id": "v1"})
    code, out = _run(
        ["meta", "set", "--home", str(vodp_home), "--non-interactive", "--video-id", "v1", "--title", "New"],
        capsys,
    )
    assert code == exitcodes.OK
    update_call = [c for c in fake_youtube.calls if c["method"] == "update"][0]
    assert update_call["kwargs"]["body"]["snippet"]["description"] == "OldDesc"
    assert update_call["kwargs"]["body"]["snippet"]["title"] == "New"
    _validate("meta_set", out)


def test_meta_set_expected_channel_id_mismatch_exits_5(vodp_home, fake_youtube, capsys):
    """F5: same guard as vods upload's, for meta set.

    Mutation-tested: also queues the happy path's snippet-readback+update
    responses (update_snippet() with no current_snippet passed in fetches
    its own snippet first), so removing the guard would make this
    genuinely succeed instead of coincidentally erroring on a missing
    fake response."""
    _seed_valid_token(vodp_home)
    _seed_expected_channel_profile(vodp_home, "UC_EXPECTED_NOT_ACTUAL")
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    fake_youtube.queue("videos", "list", {
        "items": [{"snippet": {"title": "Old", "description": "OldDesc", "tags": [], "categoryId": "20"}}]
    })
    fake_youtube.queue("videos", "update", {"id": "v1"})
    code, out = _run(
        ["meta", "set", "--home", str(vodp_home), "--non-interactive", "--video-id", "v1", "--title", "New"], capsys,
    )
    assert code == exitcodes.ENV_OR_API_ERROR
    assert not any(c["method"] == "update" for c in fake_youtube.calls)


def test_meta_set_description_file_reapplies_existing_footer(vodp_home, fake_youtube, capsys, tmp_path):
    """M22: touching the description re-derives + re-applies the
    [vodp:vod=...] footer from the *current* snippet instead of dropping it."""
    _seed_valid_token(vodp_home)
    fake_youtube.queue("videos", "list", {
        "items": [{"snippet": {"description": "Old body\n[vodp:vod=v1]", "title": "T", "categoryId": "22"}}]
    })
    fake_youtube.queue("videos", "update", {"id": "yt1"})
    desc_file = tmp_path / "d.txt"
    desc_file.write_text("Brand new body", encoding="utf-8")

    code, out = _run(
        ["meta", "set", "--home", str(vodp_home), "--non-interactive", "--video-id", "yt1",
         "--description-file", str(desc_file)],
        capsys,
    )
    assert code == exitcodes.OK
    update_call = [c for c in fake_youtube.calls if c["method"] == "update"][0]
    assert update_call["kwargs"]["body"]["snippet"]["description"] == "Brand new body\n[vodp:vod=v1]"


def test_meta_set_description_file_conflicting_footer_raises(vodp_home, fake_youtube, capsys, tmp_path):
    """M22: `meta set` must never silently reassign a video's tracked
    VOD identity to a different one."""
    _seed_valid_token(vodp_home)
    fake_youtube.queue("videos", "list", {
        "items": [{"snippet": {"description": "Old body\n[vodp:vod=v1]", "title": "T", "categoryId": "22"}}]
    })
    desc_file = tmp_path / "d.txt"
    desc_file.write_text("New body\n[vodp:vod=v2]", encoding="utf-8")

    code, out = _run(
        ["meta", "set", "--home", str(vodp_home), "--non-interactive", "--video-id", "yt1",
         "--description-file", str(desc_file)],
        capsys,
    )
    assert code == exitcodes.ARG_ERROR
    assert not any(c["method"] == "update" for c in fake_youtube.calls)


def test_meta_set_playlist_id_inserts_playlist_line_and_keeps_footer(vodp_home, fake_youtube, capsys):
    _seed_valid_token(vodp_home)
    fake_youtube.queue("videos", "list", {
        "items": [{"snippet": {"description": "Body\n[vodp:vod=v1]", "title": "T", "categoryId": "22"}}]
    })
    fake_youtube.queue("videos", "update", {"id": "yt1"})

    code, out = _run(
        ["meta", "set", "--home", str(vodp_home), "--non-interactive", "--video-id", "yt1",
         "--playlist-id", "PL1"],
        capsys,
    )
    assert code == exitcodes.OK
    update_call = [c for c in fake_youtube.calls if c["method"] == "update"][0]
    description = update_call["kwargs"]["body"]["snippet"]["description"]
    assert "プレイリスト: https://www.youtube.com/playlist?list=PL1" in description
    assert description.splitlines()[-1] == "[vodp:vod=v1]"


def test_meta_set_playlist_id_applies_even_without_existing_footer(vodp_home, fake_youtube, capsys):
    """Codex review, major #6: a video with no prior vodp footer must
    still get the playlist line applied -- not silently dropped."""
    _seed_valid_token(vodp_home)
    fake_youtube.queue("videos", "list", {
        "items": [{"snippet": {"description": "Plain body, no footer", "title": "T", "categoryId": "22"}}]
    })
    fake_youtube.queue("videos", "update", {"id": "yt1"})

    code, out = _run(
        ["meta", "set", "--home", str(vodp_home), "--non-interactive", "--video-id", "yt1",
         "--playlist-id", "PL1"],
        capsys,
    )
    assert code == exitcodes.OK
    update_call = [c for c in fake_youtube.calls if c["method"] == "update"][0]
    assert "プレイリスト: https://www.youtube.com/playlist?list=PL1" in update_call["kwargs"]["body"]["snippet"]["description"]
    list_calls = [c for c in fake_youtube.calls if c["method"] == "list"]
    assert len(list_calls) == 1  # single GET, no redundant re-fetch inside update_snippet


def test_meta_set_no_description_touch_when_neither_file_nor_playlist_given(vodp_home, fake_youtube, capsys):
    """Baseline: without --description-file/--playlist-id, description
    stays untouched (merge_snippet behavior unaffected by M22 additions)."""
    _seed_valid_token(vodp_home)
    fake_youtube.queue("videos", "list", {
        "items": [{"snippet": {"description": "Body\n[vodp:vod=v1]", "title": "T", "categoryId": "22"}}]
    })
    fake_youtube.queue("videos", "update", {"id": "yt1"})
    code, out = _run(
        ["meta", "set", "--home", str(vodp_home), "--non-interactive", "--video-id", "yt1", "--tags", "a,b"],
        capsys,
    )
    assert code == exitcodes.OK
    update_call = [c for c in fake_youtube.calls if c["method"] == "update"][0]
    assert update_call["kwargs"]["body"]["snippet"]["description"] == "Body\n[vodp:vod=v1]"


def test_meta_set_privacy_is_rejected_not_silently_ignored(vodp_home, fake_youtube, capsys):
    """R9 (Codex/tester review round 2): `meta set --privacy` used to
    parse fine, exit 0, and just never do anything with the flag --
    silently misleading an operator into thinking it changed the
    video's privacy. Must now refuse (exit 2) and point at
    `videos publish`, which actually performs+verifies the change."""
    _seed_valid_token(vodp_home)
    code, out = _run(
        ["meta", "set", "--home", str(vodp_home), "--non-interactive",
         "--video-id", "yt1", "--privacy", "public"], capsys,
    )
    assert code == exitcodes.ARG_ERROR
    assert not any(c["method"] in ("update", "list") for c in fake_youtube.calls)


def test_meta_set_privacy_rejected_even_in_dry_run(vodp_home, fake_youtube, capsys):
    _seed_valid_token(vodp_home)
    code, out = _run(
        ["meta", "set", "--home", str(vodp_home), "--dry-run",
         "--video-id", "yt1", "--privacy", "public"], capsys,
    )
    assert code == exitcodes.ARG_ERROR


# -- playlist -----------------------------------------------------------------

def test_playlist_list(vodp_home, fake_youtube, capsys):
    _seed_valid_token(vodp_home)
    fake_youtube.queue("playlists", "list", {"items": [{"id": "PL1", "snippet": {"title": "Archive"}}]})
    code, out = _run(["playlist", "list", "--home", str(vodp_home), "--non-interactive"], capsys)
    assert code == exitcodes.OK
    assert out["videos"][0]["playlist_id"] == "PL1"
    _validate("playlist_list", out)


def test_playlist_create_conflict_exits_2(vodp_home, fake_youtube, capsys):
    _seed_valid_token(vodp_home)
    fake_youtube.queue("playlists", "list", {"items": [{"id": "PL1", "snippet": {"title": "Archive"}}]})
    code, out = _run(
        ["playlist", "create", "--home", str(vodp_home), "--non-interactive", "--name", "Archive"], capsys
    )
    assert code == exitcodes.ARG_ERROR


def test_playlist_create_success(vodp_home, fake_youtube, capsys):
    """R11: playlist_create.json had no real-output validation test at
    all before this round (the only existing test was the exit-2
    conflict case, which never reaches the JSON envelope path)."""
    _seed_valid_token(vodp_home)
    fake_youtube.queue("playlists", "list", {"items": []})
    fake_youtube.queue("playlists", "insert", {"id": "PL_NEW"})
    code, out = _run(
        ["playlist", "create", "--home", str(vodp_home), "--non-interactive", "--name", "Archive"], capsys
    )
    assert code == exitcodes.OK
    assert out["videos"][0]["playlist_id"] == "PL_NEW"
    _validate("playlist_create", out)


def test_playlist_create_expected_channel_id_mismatch_exits_5(vodp_home, fake_youtube, capsys):
    """F5: same guard as vods upload's, for playlist create.

    Mutation-tested: also queues the happy path's playlists.list/insert
    responses (test_playlist_create_success), so removing the guard
    would make this genuinely succeed instead of coincidentally erroring
    on a missing fake response."""
    _seed_valid_token(vodp_home)
    _seed_expected_channel_profile(vodp_home, "UC_EXPECTED_NOT_ACTUAL")
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    fake_youtube.queue("playlists", "list", {"items": []})
    fake_youtube.queue("playlists", "insert", {"id": "PL_NEW"})
    code, out = _run(
        ["playlist", "create", "--home", str(vodp_home), "--non-interactive", "--name", "Archive"], capsys
    )
    assert code == exitcodes.ENV_OR_API_ERROR
    assert not any(c["method"] == "insert" for c in fake_youtube.calls)


def test_playlist_delete_readback_not_found_never_calls_delete(vodp_home, fake_youtube, capsys):
    """R5 misdeletion guard: ``playlists.list`` succeeding but not
    containing --playlist-id at all (already gone, or a typo'd id) must
    refuse the delete -- previously this fell through with title=None
    and called playlists.delete anyway (Codex/tester review round 2)."""
    _seed_valid_token(vodp_home)
    fake_youtube.queue("playlists", "list", {"items": [{"id": "PL_OTHER", "snippet": {"title": "Unrelated"}}]})
    fake_youtube.queue("playlists", "delete", {})
    code, out = _run(
        ["playlist", "delete", "--home", str(vodp_home), "--non-interactive", "--playlist-id", "PL1", "--yes"],
        capsys,
    )
    assert code == exitcodes.ENV_OR_API_ERROR
    assert not any(c["method"] == "delete" for c in fake_youtube.calls)


def test_playlist_delete_success(vodp_home, fake_youtube, capsys):
    _seed_valid_token(vodp_home)
    fake_youtube.queue("playlists", "list", {"items": [{"id": "PL1", "snippet": {"title": "Archive"}}]})
    fake_youtube.queue("playlists", "delete", {})
    code, out = _run(
        ["playlist", "delete", "--home", str(vodp_home), "--non-interactive", "--playlist-id", "PL1", "--yes"],
        capsys,
    )
    assert code == exitcodes.OK
    _validate("playlist_delete", out)


def test_playlist_delete_expected_channel_id_mismatch_exits_5(vodp_home, fake_youtube, capsys):
    """F5 (team-lead follow-up, round 3): CLI-wiring regression coverage
    for playlist delete specifically.

    Mutation-tested: also queues the happy path's readback+delete
    responses (test_playlist_delete_success), so removing the guard
    would make this genuinely succeed instead of coincidentally erroring
    on a missing fake response."""
    _seed_valid_token(vodp_home)
    _seed_expected_channel_profile(vodp_home, "UC_EXPECTED_NOT_ACTUAL")
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    fake_youtube.queue("playlists", "list", {"items": [{"id": "PL1", "snippet": {"title": "Archive"}}]})
    fake_youtube.queue("playlists", "delete", {})
    code, out = _run(
        ["playlist", "delete", "--home", str(vodp_home), "--non-interactive", "--playlist-id", "PL1", "--yes"],
        capsys,
    )
    assert code == exitcodes.ENV_OR_API_ERROR
    assert not any(c["method"] == "delete" for c in fake_youtube.calls)


def test_playlist_delete_readback_not_found_refuses_even_in_dry_run(vodp_home, fake_youtube, capsys):
    """Same guard applies to the dry-run preview too -- a preview that
    can't show a real title is not a trustworthy preview."""
    _seed_valid_token(vodp_home)
    fake_youtube.queue("playlists", "list", {"items": []})
    code, out = _run(
        ["playlist", "delete", "--home", str(vodp_home), "--playlist-id", "PL1"], capsys,
    )
    assert code == exitcodes.ENV_OR_API_ERROR


def test_playlist_delete_dry_run_preview_never_saves_a_refreshed_token(vodp_home, fake_youtube, capsys, monkeypatch):
    """M7 follow-up (Codex light review): same guard as `videos delete`'s
    -- the dry-run preview must not persist a refreshed token."""
    _seed_expired_refreshable_token(vodp_home)
    before = token_path(vodp_home).read_text(encoding="utf-8")

    def fake_refresh(self, request):
        self.token = "refreshed-tok"
        self.expiry = datetime.utcnow() + timedelta(hours=1)

    monkeypatch.setattr(Credentials, "refresh", fake_refresh)
    fake_youtube.queue("playlists", "list", {"items": [{"id": "PL1", "snippet": {"title": "Archive"}}]})

    code, out = _run(["playlist", "delete", "--home", str(vodp_home), "--playlist-id", "PL1"], capsys)
    assert code == exitcodes.OK

    after = token_path(vodp_home).read_text(encoding="utf-8")
    assert before == after
    assert '"refreshed-tok"' not in after


def test_playlist_remove_item_readback_api_failure_never_calls_delete(vodp_home, fake_youtube, capsys):
    """R5 misdeletion guard: if the playlistItems readback itself fails
    (surviving HttpError -> ApiError), the exception must propagate
    before ever reaching playlistItems.delete -- confirmed by construction
    (no try/except swallows it), pinned here as an explicit AC."""
    from googleapiclient.errors import HttpError

    _seed_valid_token(vodp_home)
    fake_youtube.queue("playlistItems", "list", HttpError(
        resp=type("R", (), {"status": 404, "reason": "Not Found"})(), content=b"gone",
    ))
    fake_youtube.queue("playlistItems", "delete", {})
    code, out = _run(
        ["playlist", "remove-item", "--home", str(vodp_home), "--non-interactive",
         "--playlist-id", "PL1", "--video-id", "v1", "--yes"], capsys,
    )
    assert code == exitcodes.ENV_OR_API_ERROR
    assert not any(c["method"] == "delete" for c in fake_youtube.calls)


def test_playlist_remove_item_success(vodp_home, fake_youtube, capsys):
    _seed_valid_token(vodp_home)
    fake_youtube.queue("playlistItems", "list", {"items": [
        {"id": "PLI1", "contentDetails": {"videoId": "v1"}, "snippet": {"title": "T1"}},
    ]})
    fake_youtube.queue("playlistItems", "delete", {})
    code, out = _run(
        ["playlist", "remove-item", "--home", str(vodp_home), "--non-interactive",
         "--playlist-id", "PL1", "--video-id", "v1", "--yes"], capsys,
    )
    assert code == exitcodes.OK
    _validate("playlist_remove_item", out)


def test_playlist_remove_item_expected_channel_id_mismatch_exits_5(vodp_home, fake_youtube, capsys):
    """F5 (team-lead follow-up, round 3): CLI-wiring regression coverage
    for playlist remove-item specifically.

    Mutation-tested: also queues the happy path's list+delete responses
    (test_playlist_remove_item_success), so removing the guard would
    make this genuinely succeed instead of coincidentally erroring on a
    missing fake response."""
    _seed_valid_token(vodp_home)
    _seed_expected_channel_profile(vodp_home, "UC_EXPECTED_NOT_ACTUAL")
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    fake_youtube.queue("playlistItems", "list", {"items": [
        {"id": "PLI1", "contentDetails": {"videoId": "v1"}, "snippet": {"title": "T1"}},
    ]})
    fake_youtube.queue("playlistItems", "delete", {})
    code, out = _run(
        ["playlist", "remove-item", "--home", str(vodp_home), "--non-interactive",
         "--playlist-id", "PL1", "--video-id", "v1", "--yes"], capsys,
    )
    assert code == exitcodes.ENV_OR_API_ERROR
    assert not any(c["method"] == "delete" for c in fake_youtube.calls)


def test_playlist_add_dedup_skips_when_already_present(vodp_home, fake_youtube, capsys):
    _seed_valid_token(vodp_home)
    fake_youtube.queue("playlists", "list", {"items": [{"id": "PL1", "snippet": {"title": "Archive"}}]})
    fake_youtube.queue("playlistItems", "list", {"items": [{"contentDetails": {"videoId": "v1"}}]})
    code, out = _run(
        ["playlist", "add", "--home", str(vodp_home), "--non-interactive", "--name", "Archive", "--video-id", "v1"],
        capsys,
    )
    assert code == exitcodes.OK
    assert out["videos"][0]["status"] == "skipped"
    assert not any(c["method"] == "insert" for c in fake_youtube.calls if c["resource"] == "playlistItems")
    _validate("playlist_add", out)


def test_playlist_add_expected_channel_id_mismatch_exits_5(vodp_home, fake_youtube, capsys):
    """F5 (team-lead follow-up, round 3): CLI-wiring regression coverage
    for playlist add specifically.

    Mutation-tested: also queues a genuine "not already present" happy
    path (playlists.list finds PL1, playlistItems.list is empty ->
    add_video_to_playlist() would actually be reached and would need its
    own queued response), so removing the guard would make this
    genuinely succeed instead of coincidentally erroring on a missing
    fake response."""
    _seed_valid_token(vodp_home)
    _seed_expected_channel_profile(vodp_home, "UC_EXPECTED_NOT_ACTUAL")
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    fake_youtube.queue("playlists", "list", {"items": [{"id": "PL1", "snippet": {"title": "Archive"}}]})
    fake_youtube.queue("playlistItems", "list", {"items": []})
    fake_youtube.queue("playlistItems", "insert", {"id": "PLI_NEW"})
    code, out = _run(
        ["playlist", "add", "--home", str(vodp_home), "--non-interactive", "--name", "Archive", "--video-id", "v1"],
        capsys,
    )
    assert code == exitcodes.ENV_OR_API_ERROR
    assert not any(c["method"] == "insert" for c in fake_youtube.calls)


# -- auth migrate -------------------------------------------------------------

def test_auth_migrate_pickle(vodp_home, tmp_path, capsys):
    creds = Credentials(
        token="pickled", refresh_token="r", token_uri="https://oauth2.googleapis.com/token",
        client_id="cid", client_secret="cs", scopes=list(SCOPES),
    )
    pickle_path = tmp_path / "token.pickle"
    with open(pickle_path, "wb") as f:
        pickle.dump(creds, f)
    code, out = _run(
        ["auth", "migrate", "--home", str(vodp_home), "--pickle-path", str(pickle_path)], capsys
    )
    assert code == exitcodes.OK
    assert token_path(vodp_home).exists()
    _validate("auth_migrate", out)


def test_auth_login_rejects_non_interactive():
    from vodp.cli.main import main as _main
    code = _main(["auth", "login", "--non-interactive"])
    assert code == exitcodes.ARG_ERROR


def test_auth_login_with_already_valid_token_is_a_noop_success(vodp_home, capsys):
    """R11: auth_login.json had no real-output validation test at all --
    the only existing `auth login` test never reaches the JSON envelope
    path (--non-interactive always refuses before that). A pre-seeded,
    still-valid token lets ``authenticate()`` return immediately (state
    "valid", design.md §5) without opening a browser, fully offline."""
    _seed_valid_token(vodp_home)
    code, out = _run(["auth", "login", "--home", str(vodp_home)], capsys)
    assert code == exitcodes.OK
    assert out["videos"][0]["token_path"] == str(token_path(vodp_home))
    _validate("auth_login", out)
