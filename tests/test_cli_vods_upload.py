"""``vodp vods upload`` end-to-end (M4/M7/M8/M15/M17/M22 integrated AC, S6).

Fully offline: Twitch via FakeSession, YouTube via FakeYouTube, yt-dlp via
FakeYDL, ffprobe via a tiny local shell script on PATH (no real ffprobe
binary required -- design.md §10's "ffprobe subprocess.run" fake point).
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta
from pathlib import Path

import jsonschema
import pytest

import vodp.cli.commands.vods as vods_module
from tests.fakes import FakeResponse, FakeSession, FakeYDL, FakeYouTube
from vodp.cli.main import main
from vodp.core import exitcodes
from vodp.infra.auth import SCOPES, token_path
from vodp.infra.ledger import Ledger, compute_intent_hash, resolve_ledger_path

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "src" / "vodp" / "schemas"


def _validate(command_schema_name, envelope):
    """R11 (Codex/tester review round 2): vods_upload.json had no
    real-output validation test at all before this round."""
    schema = json.loads((SCHEMAS_DIR / f"{command_schema_name}.json").read_text())
    jsonschema.validate(instance=envelope, schema=schema)


def _twitch_video(vid, created_at_iso, duration="10m0s"):
    return {
        "id": vid, "title": f"Stream {vid}", "created_at": created_at_iso,
        "duration": duration, "url": f"https://twitch.tv/videos/{vid}",
    }


def _seed_env_and_token(home):
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text(
        "TWITCH_CLIENT_ID=cid\nTWITCH_CLIENT_SECRET=csecret\nTWITCH_CHANNEL_LOGIN=chan\n", encoding="utf-8",
    )
    token_path(home).parent.mkdir(parents=True, exist_ok=True)
    token_path(home).write_text(json.dumps({
        "token": "tok", "refresh_token": "refresh", "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "cid", "client_secret": "csecret", "scopes": list(SCOPES),
        "expiry": (datetime.utcnow() + timedelta(hours=1)).isoformat(),
    }), encoding="utf-8")
    # the packaged default profile's earliest_date is a placeholder set to
    # "today" (migration-day guard, M21改 §11); override it here so fixed
    # 2026-07-01 test fixtures aren't filtered out by that guard.
    (home / "profiles").mkdir(parents=True, exist_ok=True)
    (home / "profiles" / "default.toml").write_text(
        """
        [meta]
        title_template = "{title}"
        description_template = "{title}\\n\\n{vod_url}"
        tags = []
        category_id = "22"

        [limits]
        earliest_date = "2000-01-01"
        """,
        encoding="utf-8",
    )


@pytest.fixture
def fake_ffprobe(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "ffprobe"
    script.write_text("#!/bin/sh\necho 65\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ["PATH"])
    return script


@pytest.fixture
def fake_twitch(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(vods_module.requests, "Session", lambda: session)
    return session


@pytest.fixture
def fake_youtube(monkeypatch):
    fake = FakeYouTube()
    import vodp.infra.youtube as yt_module
    monkeypatch.setattr(yt_module, "build", lambda *a, **kw: fake)
    return fake


@pytest.fixture(autouse=True)
def fake_ydl(monkeypatch):
    """Force every download_vod() call made from the vods command module
    to use FakeYDL, without ever touching real yt-dlp/network."""
    original = vods_module.downloader_infra.download_vod

    def patched(url, download_dir, vod_id, **kwargs):
        kwargs.setdefault("ydl_class", FakeYDL)
        return original(url, download_dir, vod_id, **kwargs)

    monkeypatch.setattr(vods_module.downloader_infra, "download_vod", patched)


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


def test_upload_happy_path_private_insert_publish_verify(vodp_home, fake_ffprobe, fake_twitch, fake_youtube, capsys, monkeypatch):
    monkeypatch.setattr("vodp.infra.youtube.time.sleep", lambda s: None)
    _seed_env_and_token(vodp_home)

    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [_twitch_video("v1", "2026-07-01T10:00:00Z")], "pagination": {},
    }))

    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    fake_youtube.queue("videos", "insert", {"id": "yt1"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"uploadStatus": "processed"}}]})  # post-insert status check
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})  # publish() read
    fake_youtube.queue("videos", "update", {"id": "yt1"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})  # verify_privacy readback

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01", "--privacy", "private",
    ], capsys)

    assert code == exitcodes.OK, out
    assert out["videos"][0]["status"] == "uploaded"
    assert out["videos"][0]["youtube_video_id"] == "yt1"
    _validate("vods_upload", out)

    ledger_path = resolve_ledger_path(vodp_home, "UCabc", register=False)
    ledger = Ledger(ledger_path, channel_id="UCabc")
    assert ledger.get_current("v1")["stage"] == "completed"


def test_upload_f5_expected_channel_id_mismatch_exits_5_before_any_insert(
    vodp_home, fake_ffprobe, fake_twitch, fake_youtube, capsys, monkeypatch,
):
    """F5 (Codex/tester review round 2, differed-items follow-up,
    requirements.md 推奨S7): a profile pinning ``[channel]
    expected_channel_id`` must refuse to run against a *different*
    authenticated account -- exit 5, before any download/insert, so an
    unattended run never silently uploads to the wrong channel.

    Mutation-tested (team-lead follow-up, round 3): every fake response
    the *happy path* would need is queued too (insert/status/privacy/
    update), so that removing the verify_expected_channel() call would
    make this run genuinely *succeed* (exit 0) instead of coincidentally
    hitting some other missing-fake AssertionError that also happens to
    map to exit 5 through main.py's generic exception handler -- an
    earlier version of this test only queued responses up through
    channels.list, so it "passed" even with the check removed entirely."""
    monkeypatch.setattr("vodp.infra.youtube.time.sleep", lambda s: None)
    _seed_env_and_token(vodp_home)
    (vodp_home / "profiles" / "default.toml").write_text(
        """
        [meta]
        title_template = "{title}"
        description_template = "{title}\\n\\n{vod_url}"
        tags = []
        category_id = "22"

        [limits]
        earliest_date = "2000-01-01"

        [channel]
        expected_channel_id = "UC_EXPECTED_NOT_ACTUAL"
        """,
        encoding="utf-8",
    )
    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [_twitch_video("v1", "2026-07-01T10:00:00Z")], "pagination": {},
    }))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})  # actually authenticated as UCabc
    fake_youtube.queue("videos", "insert", {"id": "yt1"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"uploadStatus": "processed"}}]})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})
    fake_youtube.queue("videos", "update", {"id": "yt1"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01",
    ], capsys)

    assert code == exitcodes.ENV_OR_API_ERROR
    assert not any(c["resource"] == "videos" and c["method"] == "insert" for c in fake_youtube.calls)


def test_upload_f5_expected_channel_id_match_proceeds_normally(
    vodp_home, fake_ffprobe, fake_twitch, fake_youtube, capsys, monkeypatch,
):
    """Same profile setting, but matching the authenticated account --
    must be a no-op guard, not an accidental universal block."""
    monkeypatch.setattr("vodp.infra.youtube.time.sleep", lambda s: None)
    _seed_env_and_token(vodp_home)
    (vodp_home / "profiles" / "default.toml").write_text(
        """
        [meta]
        title_template = "{title}"
        description_template = "{title}\\n\\n{vod_url}"
        tags = []
        category_id = "22"

        [limits]
        earliest_date = "2000-01-01"

        [channel]
        expected_channel_id = "UCabc"
        """,
        encoding="utf-8",
    )
    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [_twitch_video("v1", "2026-07-01T10:00:00Z")], "pagination": {},
    }))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    fake_youtube.queue("videos", "insert", {"id": "yt1"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"uploadStatus": "processed"}}]})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})
    fake_youtube.queue("videos", "update", {"id": "yt1"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01",
    ], capsys)

    assert code == exitcodes.OK, out
    assert out["videos"][0]["status"] == "uploaded"


def test_upload_c1_crash_before_reserve_resumes_as_plain_fresh_discovery(
    vodp_home, fake_ffprobe, fake_twitch, fake_youtube, capsys, monkeypatch,
):
    """§4 C1: a crash before ``ledger.reserve()`` ever ran leaves no row at
    all -- there is nothing to resume *from*, so the next run must
    discover and process the VOD exactly like it had never been touched
    (design.md's own gloss for C1 is literally "通常", i.e. the ordinary
    path). This asserts that starting condition explicitly (no residual
    ledger row pre-existing) rather than assuming it."""
    monkeypatch.setattr("vodp.infra.youtube.time.sleep", lambda s: None)
    _seed_env_and_token(vodp_home)

    ledger_path = resolve_ledger_path(vodp_home, "UCabc", register=False)
    assert not ledger_path.exists(), "precondition: nothing was ever reserved before the simulated crash"

    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [_twitch_video("v1", "2026-07-01T10:00:00Z")], "pagination": {},
    }))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    fake_youtube.queue("videos", "insert", {"id": "yt1"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"uploadStatus": "processed"}}]})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})
    fake_youtube.queue("videos", "update", {"id": "yt1"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01",
    ], capsys)

    assert code == exitcodes.OK, out
    assert out["videos"][0]["status"] == "uploaded"
    insert_calls = [c for c in fake_youtube.calls if c["resource"] == "videos" and c["method"] == "insert"]
    assert len(insert_calls) == 1
    ledger = Ledger(ledger_path, channel_id="UCabc")
    assert ledger.get_current("v1")["stage"] == "completed"


def test_upload_c2_stale_part_artifact_from_interrupted_download_is_harmless(
    vodp_home, fake_ffprobe, fake_twitch, fake_youtube, capsys, monkeypatch,
):
    """§4 C2: a crash mid-download leaves a stale ``.part`` file behind
    (yt-dlp's own convention) for a VOD whose ledger row is still at
    ``reserved`` (insert was never attempted). Re-running must clean up
    and complete a fresh download+insert exactly once -- the leftover
    ``.part`` must never be mistaken for a finished, reusable artifact."""
    monkeypatch.setattr("vodp.infra.youtube.time.sleep", lambda s: None)
    _seed_env_and_token(vodp_home)
    downloads = vodp_home / "downloads"
    downloads.mkdir(parents=True)
    stale_part = downloads / "vodp-v1.mp4.part"
    stale_part.write_bytes(b"\x00" * 10)  # truncated, mid-transfer bytes from the crashed attempt

    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [_twitch_video("v1", "2026-07-01T10:00:00Z")], "pagination": {},
    }))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    fake_youtube.queue("videos", "insert", {"id": "yt1"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"uploadStatus": "processed"}}]})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})
    fake_youtube.queue("videos", "update", {"id": "yt1"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01",
    ], capsys)

    assert code == exitcodes.OK, out
    assert out["videos"][0]["status"] == "uploaded"
    insert_calls = [c for c in fake_youtube.calls if c["resource"] == "videos" and c["method"] == "insert"]
    assert len(insert_calls) == 1  # the stale .part never caused a second attempted insert
    # the finished (non-.part) download is always unlinked after insert either way
    assert not (downloads / "vodp-v1.mp4").exists()


def test_upload_c9_already_finalized_download_file_present_before_insert(
    vodp_home, fake_ffprobe, fake_twitch, fake_youtube, capsys, monkeypatch,
):
    """§4 C9: a crash after yt-dlp already renamed the ``.part`` file to
    its final name but before ``videos.insert`` was ever called -- the
    finished artifact from the previous attempt is already sitting at the
    exact deterministic path a fresh download would also produce. This
    must not cause any error or double-insert; the pipeline completes
    exactly once regardless of whether that pre-existing file is reused
    outright or (as with this offline fake) transparently rewritten."""
    monkeypatch.setattr("vodp.infra.youtube.time.sleep", lambda s: None)
    _seed_env_and_token(vodp_home)
    downloads = vodp_home / "downloads"
    downloads.mkdir(parents=True)
    (downloads / "vodp-v1.mp4").write_bytes(b"already finished from a prior crashed attempt")

    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [_twitch_video("v1", "2026-07-01T10:00:00Z")], "pagination": {},
    }))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    fake_youtube.queue("videos", "insert", {"id": "yt1"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"uploadStatus": "processed"}}]})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})
    fake_youtube.queue("videos", "update", {"id": "yt1"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01",
    ], capsys)

    assert code == exitcodes.OK, out
    assert out["videos"][0]["status"] == "uploaded"
    insert_calls = [c for c in fake_youtube.calls if c["resource"] == "videos" and c["method"] == "insert"]
    assert len(insert_calls) == 1


def test_upload_second_run_skips_already_uploaded(vodp_home, fake_ffprobe, fake_twitch, fake_youtube, capsys, monkeypatch):
    monkeypatch.setattr("vodp.infra.youtube.time.sleep", lambda s: None)
    _seed_env_and_token(vodp_home)

    for _ in range(2):
        fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
        fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
        fake_twitch.queue("/videos", FakeResponse(200, {
            "data": [_twitch_video("v1", "2026-07-01T10:00:00Z")], "pagination": {},
        }))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    fake_youtube.queue("videos", "insert", {"id": "yt1"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"uploadStatus": "processed"}}]})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})
    fake_youtube.queue("videos", "update", {"id": "yt1"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})

    code1, out1 = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01", "--privacy", "private",
    ], capsys)
    assert code1 == exitcodes.OK

    code2, out2 = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01", "--privacy", "private",
    ], capsys)
    assert code2 == exitcodes.OK
    assert out2["videos"][0]["status"] == "skipped"
    assert out2["videos"][0]["reason_code"] == "already_uploaded"
    insert_calls = [c for c in fake_youtube.calls if c["resource"] == "videos" and c["method"] == "insert"]
    assert len(insert_calls) == 1  # never re-inserted


def test_upload_too_long_skips_without_download(vodp_home, fake_ffprobe, fake_twitch, fake_youtube, capsys):
    _seed_env_and_token(vodp_home)
    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [_twitch_video("v1", "2026-07-01T10:00:00Z", duration="13h0m0s")], "pagination": {},
    }))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01",
    ], capsys)
    assert out["videos"][0]["reason_code"] == "too_long"
    assert not any(c["resource"] == "videos" and c["method"] == "insert" for c in fake_youtube.calls)


def test_upload_size_exceeded_after_download_skips_insert_and_marks_rejected(
    vodp_home, fake_ffprobe, fake_twitch, fake_youtube, capsys, monkeypatch,
):
    """M14 AC (R10, Codex/tester review round 2): unlike ``too_long``
    (known from Twitch metadata *before* any download), the 256GB size
    cap is a *post-download* check (``_process_fresh``, after ffprobe)
    -- the file has already landed on disk once this check runs. This is
    a permanent_failure/`rejected` ledger outcome, never re-attempted on
    a later resumed run, and insert must never be reached."""
    monkeypatch.setattr(vods_module, "MAX_VIDEO_BYTES", 5)  # FakeYDL writes 17 bytes -- always "too big" here
    _seed_env_and_token(vodp_home)
    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [_twitch_video("v1", "2026-07-01T10:00:00Z")], "pagination": {},
    }))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01",
    ], capsys)

    assert out["videos"][0]["status"] == "failed"
    assert out["videos"][0]["reason_code"] == "size_exceeded"
    assert code == exitcodes.PARTIAL_FAILURE
    assert not any(c["resource"] == "videos" and c["method"] == "insert" for c in fake_youtube.calls)
    # the oversized download must not linger in downloads/ either.
    assert not (vodp_home / "downloads" / "vodp-v1.mp4").exists()

    ledger_path = resolve_ledger_path(vodp_home, "UCabc", register=False)
    ledger = Ledger(ledger_path, channel_id="UCabc")
    current = ledger.get_current("v1")
    assert current["stage"] == "rejected"
    assert current["outcome"] == "permanent_failure"


def test_upload_dry_run_makes_no_writes(vodp_home, fake_twitch, fake_youtube, capsys):
    _seed_env_and_token(vodp_home)
    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [_twitch_video("v1", "2026-07-01T10:00:00Z")], "pagination": {},
    }))
    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01", "--dry-run",
    ], capsys)
    assert code == exitcodes.OK
    assert out["videos"][0]["reason_code"] == "dry_run"
    assert fake_youtube.calls == []
    assert not (vodp_home / "ledger").exists()
    assert not (vodp_home / "logs").exists()


def _seed_env_token_and_profile_with_earliest_date(home, earliest_date):
    """Like _seed_env_and_token, but with a caller-chosen (not the fixed
    2000-01-01 override) [limits] earliest_date -- needed to actually
    reproduce the E2E-found bug (team-lead follow-up): a real profile
    with a *later* cutoff that excludes some VODs but not others."""
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text(
        "TWITCH_CLIENT_ID=cid\nTWITCH_CLIENT_SECRET=csecret\nTWITCH_CHANNEL_LOGIN=chan\n", encoding="utf-8",
    )
    token_path(home).parent.mkdir(parents=True, exist_ok=True)
    token_path(home).write_text(json.dumps({
        "token": "tok", "refresh_token": "refresh", "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "cid", "client_secret": "csecret", "scopes": list(SCOPES),
        "expiry": (datetime.utcnow() + timedelta(hours=1)).isoformat(),
    }), encoding="utf-8")
    (home / "profiles").mkdir(parents=True, exist_ok=True)
    (home / "profiles" / "default.toml").write_text(
        f"""
        [meta]
        title_template = "{{title}}"
        description_template = "{{title}}\\n\\n{{vod_url}}"
        tags = []
        category_id = "22"

        [limits]
        earliest_date = "{earliest_date}"
        """,
        encoding="utf-8",
    )


def test_upload_dry_run_applies_earliest_date_cutoff_same_as_real_run(
    vodp_home, fake_ffprobe, fake_twitch, fake_youtube, capsys, monkeypatch,
):
    """team-lead E2E follow-up: real E2E testing found dry-run never
    applied the profile's [limits] earliest_date cutoff at all -- it
    "promised" a VOD (dry_run skip) that the real run then silently
    dropped (0 targets, exit 3), violating M7's "dry-run is a complete
    preview" guarantee. Two VODs: one before the cutoff (must be
    excluded, reported as out_of_range in *both* paths) and one at/after
    it (must be the only one either path treats as an actual candidate)."""
    monkeypatch.setattr("vodp.infra.youtube.time.sleep", lambda s: None)
    _seed_env_token_and_profile_with_earliest_date(vodp_home, "2026-07-27")
    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [
            _twitch_video("v_too_old", "2026-07-25T10:00:00Z"),  # before the 2026-07-27 cutoff
            _twitch_video("v_ok", "2026-07-28T10:00:00Z"),  # at/after the cutoff
        ],
        "pagination": {},
    }))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})

    dry_code, dry_out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-25", "2026-07-28", "--dry-run",
    ], capsys)

    dry_by_id = {v["twitch_vod_id"]: v for v in dry_out["videos"]}
    assert dry_by_id["v_too_old"]["status"] == "skipped"
    assert dry_by_id["v_too_old"]["reason_code"] == "out_of_range"
    assert dry_by_id["v_ok"]["reason_code"] == "dry_run"
    assert dry_code == exitcodes.OK
    dry_run_actionable_ids = {v["twitch_vod_id"] for v in dry_out["videos"] if v["reason_code"] == "dry_run"}

    # Same VODs, same range, for real this time.
    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [
            _twitch_video("v_too_old", "2026-07-25T10:00:00Z"),
            _twitch_video("v_ok", "2026-07-28T10:00:00Z"),
        ],
        "pagination": {},
    }))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    fake_youtube.queue("videos", "insert", {"id": "yt-ok"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"uploadStatus": "processed"}}]})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})
    fake_youtube.queue("videos", "update", {"id": "yt-ok"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})

    real_code, real_out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-25", "2026-07-28",
    ], capsys)

    real_by_id = {v["twitch_vod_id"]: v for v in real_out["videos"]}
    assert real_by_id["v_too_old"]["status"] == "skipped"
    assert real_by_id["v_too_old"]["reason_code"] == "out_of_range"  # same reason_code, not silently dropped
    assert real_by_id["v_ok"]["status"] == "uploaded"
    assert real_code == exitcodes.OK
    _validate("vods_upload", real_out)

    # The core AC: dry-run's set of *actionable* candidates (dry_run
    # reason_code) is exactly the real run's set of actually-attempted
    # candidates (anything other than out_of_range) -- dry-run is a
    # complete, accurate preview, never promising something the real run
    # then silently excludes.
    real_actionable_ids = {vid for vid, v in real_by_id.items() if v["reason_code"] != "out_of_range"}
    assert dry_run_actionable_ids == real_actionable_ids == {"v_ok"}
    insert_calls = [c for c in fake_youtube.calls if c["resource"] == "videos" and c["method"] == "insert"]
    assert len(insert_calls) == 1


def test_upload_dry_run_all_candidates_out_of_range_exits_3_not_0(
    vodp_home, fake_ffprobe, fake_twitch, fake_youtube, capsys, monkeypatch,
):
    """The exact E2E scenario reported: a run where every discovered VOD
    is excluded by earliest_date must still be NO_TARGETS (3), in *both*
    dry-run and the real path -- the out_of_range entries are
    informational only, never "found a target"."""
    monkeypatch.setattr("vodp.infra.youtube.time.sleep", lambda s: None)
    _seed_env_token_and_profile_with_earliest_date(vodp_home, "2026-07-27")
    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [_twitch_video("v_too_old", "2026-07-25T10:00:00Z")], "pagination": {},
    }))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})

    dry_code, dry_out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-25", "2026-07-25", "--dry-run",
    ], capsys)
    assert dry_code == exitcodes.NO_TARGETS
    assert dry_out["videos"][0]["reason_code"] == "out_of_range"

    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [_twitch_video("v_too_old", "2026-07-25T10:00:00Z")], "pagination": {},
    }))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})

    real_code, real_out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-25", "2026-07-25",
    ], capsys)
    assert real_code == exitcodes.NO_TARGETS
    assert real_out["videos"][0]["reason_code"] == "out_of_range"  # explicit, not silently 0 videos[]


def test_upload_zero_results_without_allow_empty_exits_3(vodp_home, fake_twitch, fake_youtube, capsys):
    """R13 AC (Codex/tester review round 2): `vods upload`'s real (non
    dry-run) path with zero candidate VODs in range must exit 3
    (NO_TARGETS) by default -- previously only ``vods list``'s exit-3
    behavior (and the dry-run branch's) had test coverage; the *real*
    upload path's own ``not videos and not settings.allow_empty`` branch
    (feeding into exitcodes.combine()) never did."""
    _seed_env_and_token(vodp_home)
    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {"data": [], "pagination": {}}))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01",
    ], capsys)

    assert code == exitcodes.NO_TARGETS
    assert out["videos"] == []


def test_upload_zero_results_with_allow_empty_exits_0_not_3(vodp_home, fake_twitch, fake_youtube, capsys):
    """R13 AC: ``--allow-empty`` must actually flip that same zero-target
    run to exit 0 -- not just be accepted by argparse and silently
    ignored (this is the real (non dry-run) upload path; the dry-run
    branch has its own, separate `settings.allow_empty` check that this
    test does not exercise)."""
    _seed_env_and_token(vodp_home)
    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {"data": [], "pagination": {}}))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01", "--allow-empty",
    ], capsys)

    assert code == exitcodes.OK
    assert out["videos"] == []


def test_upload_max_uploads_limits_inserts(vodp_home, fake_ffprobe, fake_twitch, fake_youtube, capsys, monkeypatch):
    monkeypatch.setattr("vodp.infra.youtube.time.sleep", lambda s: None)
    _seed_env_and_token(vodp_home)
    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [
            _twitch_video("v2", "2026-07-01T12:00:00Z"),
            _twitch_video("v1", "2026-07-01T10:00:00Z"),
        ],
        "pagination": {},
    }))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    fake_youtube.queue("videos", "insert", {"id": "yt-only-one"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"uploadStatus": "processed"}}]})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})
    fake_youtube.queue("videos", "update", {"id": "yt-only-one"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01", "--max-uploads", "1",
    ], capsys)
    statuses = {v["twitch_vod_id"]: v["status"] for v in out["videos"]}
    assert statuses["v2"] == "uploaded"
    assert statuses["v1"] == "skipped"
    insert_calls = [c for c in fake_youtube.calls if c["resource"] == "videos" and c["method"] == "insert"]
    assert len(insert_calls) == 1


def test_upload_second_concurrent_run_fails_lock(vodp_home, fake_twitch, fake_youtube, capsys):
    """M16: a channel lock already held by a first (still-running) process
    makes a second invocation exit 5 instead of blocking or double-writing."""
    _seed_env_and_token(vodp_home)
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))

    from vodp.infra.locking import exclusive_lock
    lock_path = vodp_home / "run" / "channels" / "UCabc.lock"
    with exclusive_lock(lock_path):
        code, out = _run([
            "vods", "upload", "--home", str(vodp_home), "--non-interactive",
            "--range", "2026-07-01", "2026-07-01",
        ], capsys)
    assert code == exitcodes.ENV_OR_API_ERROR


def _seed_insert_inflight_row(vodp_home, channel_id, twitch_vod_id):
    ledger_path = resolve_ledger_path(vodp_home, channel_id)
    ledger = Ledger(ledger_path, channel_id=channel_id)
    intent = {
        "title": f"Stream {twitch_vod_id}", "description": f"desc\n[vodp:vod={twitch_vod_id}]",
        "tags": [], "category_id": "22", "privacy": "private", "playlist": None, "playlist_privacy": "public",
    }
    ledger.reserve(twitch_vod_id=twitch_vod_id, twitch_user_id="999", profile="default", intent=intent)
    ledger.advance(twitch_vod_id=twitch_vod_id, stage="insert_inflight", expected_stage="reserved")
    return ledger


def test_upload_resumes_insert_inflight_via_footer_marker_when_found(
    vodp_home, fake_ffprobe, fake_twitch, fake_youtube, capsys, monkeypatch,
):
    """§4 C3: insert response lost, but the video actually made it to
    YouTube (footer marker found) -- resumes forward, never re-inserts."""
    monkeypatch.setattr("vodp.infra.youtube.time.sleep", lambda s: None)
    _seed_env_and_token(vodp_home)
    _seed_insert_inflight_row(vodp_home, "UCabc", "v1")

    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {"data": [], "pagination": {}}))  # nothing new in range

    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    # resumable-queue reconciliation: search uploads playlist for the footer marker
    fake_youtube.queue("channels", "list", {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]})
    fake_youtube.queue("playlistItems", "list", {"items": [{"contentDetails": {"videoId": "yt1"}}]})
    fake_youtube.queue("videos", "list", {"items": [{"snippet": {"description": "desc\n[vodp:vod=v1]"}}]})
    # post-upload continuation
    fake_youtube.queue("videos", "list", {"items": [{"status": {"uploadStatus": "processed"}}]})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})
    fake_youtube.queue("videos", "update", {"id": "yt1"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-02", "2026-07-02",
    ], capsys)

    assert code == exitcodes.OK, out
    assert out["videos"][0]["status"] == "uploaded"
    assert out["videos"][0]["youtube_video_id"] == "yt1"
    assert not any(c["resource"] == "videos" and c["method"] == "insert" for c in fake_youtube.calls)

    ledger_path = resolve_ledger_path(vodp_home, "UCabc", register=False)
    ledger = Ledger(ledger_path, channel_id="UCabc")
    assert ledger.get_current("v1")["stage"] == "completed"


def test_upload_resumes_insert_inflight_marks_recovery_required_when_not_found(
    vodp_home, fake_twitch, fake_youtube, capsys,
):
    """§4 C3: insert response lost AND the video is nowhere to be found ->
    recovery_required, exit 5, never auto-reinserted."""
    _seed_env_and_token(vodp_home)
    _seed_insert_inflight_row(vodp_home, "UCabc", "v1")

    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {"data": [], "pagination": {}}))

    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    fake_youtube.queue("channels", "list", {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]})
    fake_youtube.queue("playlistItems", "list", {"items": []})  # nothing uploaded at all

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-02", "2026-07-02",
    ], capsys)

    assert code == exitcodes.ENV_OR_API_ERROR
    assert out["videos"][0]["reason_code"] == "recovery_required"
    assert not any(c["resource"] == "videos" and c["method"] == "insert" for c in fake_youtube.calls)

    ledger_path = resolve_ledger_path(vodp_home, "UCabc", register=False)
    ledger = Ledger(ledger_path, channel_id="UCabc")
    current = ledger.get_current("v1")
    assert current["stage"] == "recovery_required"
    assert current["recovery_from_stage"] == "insert_inflight"

    # the video was confirmed nowhere to be found, so a human-approved
    # `ledger resolve --action reinsert` IS allowed to restart from scratch
    # (this is the documented manual recovery path, not an auto-retry).
    ledger.resolve(twitch_vod_id="v1", action="reinsert")
    assert ledger.get_current("v1")["stage"] == "reserved"


def _fresh_intent_for(video_id, created_at_iso, *, privacy="private"):
    """Recomputes the exact intent ``_process_one_candidate`` would build
    for this VOD under ``_seed_env_and_token``'s default profile (same
    title/description template) -- C5/C6 rediscover the VOD in-range on
    the resumed run, so the seeded intent must hash-match exactly or
    ``ledger.reserve()`` would (correctly) refuse it as a changed intent."""
    from vodp.core.description import build_description_for_insert
    from vodp.core.template import render_template
    from vodp.core.timerange import JST

    created_at = datetime.fromisoformat(created_at_iso.replace("Z", "+00:00"))
    url = f"https://twitch.tv/videos/{video_id}"
    values = {
        "date": created_at.astimezone(JST).strftime("%Y-%m-%d"),
        "vod_url": url,
        "title": f"Stream {video_id}",
    }
    title = render_template("{title}", {**values, "playlist_url": ""})
    description = build_description_for_insert(
        "{title}\n\n{vod_url}", values, twitch_vod_id=video_id, playlist_url=None,
    )
    return {
        "title": title, "description": description, "tags": [], "category_id": "22",
        "privacy": privacy, "playlist_id": None,
    }


def _seed_processed_row(vodp_home, channel_id, twitch_vod_id, created_at_iso, youtube_video_id="yt1"):
    """A row that has already survived insert+the post-insert uploadStatus
    check (stage ``processed``) but never reached ``meta_verified`` --
    i.e. whatever happened next (the pre-update privacy readback, or the
    update call itself) never got durably recorded."""
    ledger_path = resolve_ledger_path(vodp_home, channel_id)
    ledger = Ledger(ledger_path, channel_id=channel_id)
    intent = _fresh_intent_for(twitch_vod_id, created_at_iso)
    ledger.reserve(twitch_vod_id=twitch_vod_id, twitch_user_id="999", profile="default", intent=intent)
    ledger.advance(twitch_vod_id=twitch_vod_id, stage="insert_inflight", expected_stage="reserved")
    ledger.advance(twitch_vod_id=twitch_vod_id, stage="uploaded", youtube_video_id=youtube_video_id,
                    expected_stage="insert_inflight")
    ledger.advance(twitch_vod_id=twitch_vod_id, stage="processed", expected_stage="uploaded")
    return ledger


def test_upload_c5_crash_during_prepublish_readback_resumes_from_processed(
    vodp_home, fake_ffprobe, fake_twitch, fake_youtube, capsys, monkeypatch,
):
    """§4 C5: the crash happened *during* the privacy readback publish()
    does before ever calling update() -- nothing about that readback is
    itself durable, so the ledger row is simply still sitting at
    ``processed``. Re-running must resume "from publish" (design.md's own
    words): re-issue the readback, call update() once, and verify --
    never touching insert again."""
    monkeypatch.setattr("vodp.infra.youtube.time.sleep", lambda s: None)
    _seed_env_and_token(vodp_home)
    _seed_processed_row(vodp_home, "UCabc", "v1", "2026-07-01T10:00:00Z")

    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    # v1 is rediscovered in-range (same VOD, same date as the original
    # crashed attempt) -- reserve()'s intent-hash match against the
    # already-processed row is what routes straight into
    # _continue_post_upload instead of re-downloading/re-inserting.
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [_twitch_video("v1", "2026-07-01T10:00:00Z")], "pagination": {},
    }))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})  # publish() readback
    fake_youtube.queue("videos", "update", {"id": "yt1"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})  # verify_privacy

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01",
    ], capsys)

    assert code == exitcodes.OK, out
    assert out["videos"][0]["status"] == "uploaded"
    assert not any(c["resource"] == "videos" and c["method"] == "insert" for c in fake_youtube.calls)
    update_calls = [c for c in fake_youtube.calls if c["resource"] == "videos" and c["method"] == "update"]
    assert len(update_calls) == 1

    ledger_path = resolve_ledger_path(vodp_home, "UCabc", register=False)
    ledger = Ledger(ledger_path, channel_id="UCabc")
    assert ledger.get_current("v1")["stage"] == "completed"


def test_upload_c6_update_response_lost_reissuing_update_is_idempotent(
    vodp_home, fake_ffprobe, fake_twitch, fake_youtube, capsys, monkeypatch,
):
    """§4 C6: the earlier crash lost ``videos.update``'s *response*, but
    the write may already have landed server-side (privacyStatus already
    equals the target). Resuming from ``processed`` re-issues update()
    unconditionally (design.md: "readback 冪等" -- update is idempotent,
    so calling it again with the same target value is always safe) and
    the post-update readback must still confirm success, never re-insert,
    and never treat "it might already be done" as a reason to skip
    verification."""
    monkeypatch.setattr("vodp.infra.youtube.time.sleep", lambda s: None)
    _seed_env_and_token(vodp_home)
    _seed_processed_row(vodp_home, "UCabc", "v1", "2026-07-01T10:00:00Z")

    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [_twitch_video("v1", "2026-07-01T10:00:00Z")], "pagination": {},
    }))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    # the target privacy ("private", the default) already reads back
    # correctly here -- simulating that the lost-response update() call
    # actually succeeded server-side before the crash.
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})  # publish() readback
    fake_youtube.queue("videos", "update", {"id": "yt1"})  # re-issued anyway -- idempotent, harmless
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})  # verify_privacy

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01",
    ], capsys)

    assert code == exitcodes.OK, out
    assert out["videos"][0]["status"] == "uploaded"
    assert not any(c["resource"] == "videos" and c["method"] == "insert" for c in fake_youtube.calls)
    update_calls = [c for c in fake_youtube.calls if c["resource"] == "videos" and c["method"] == "update"]
    assert len(update_calls) == 1

    ledger_path = resolve_ledger_path(vodp_home, "UCabc", register=False)
    ledger = Ledger(ledger_path, channel_id="UCabc")
    assert ledger.get_current("v1")["stage"] == "completed"


def test_upload_f1_crash_after_insert_resumes_across_a_range_boundary(
    vodp_home, fake_ffprobe, fake_twitch, fake_youtube, capsys, monkeypatch,
):
    """F1 (Codex/tester review round 2, differed-items follow-up): a row
    left at ``processed`` (post-insert, pre-completed) after a day-1
    crash must still resume to completion on a *later* unattended run
    whose default ``--range`` is day 2 -- day 1's VOD is never
    rediscovered by Twitch listing again, so only the range-independent
    resume queue (``ledger.resumable_entries()``, not in-range candidate
    rediscovery) can reach it. The C5/C6 tests directly above exercise
    the *same-range* rediscovery path and would not have caught this --
    tester's independent verification found that gap."""
    monkeypatch.setattr("vodp.infra.youtube.time.sleep", lambda s: None)
    _seed_env_and_token(vodp_home)
    _seed_processed_row(vodp_home, "UCabc", "v1", "2026-07-01T10:00:00Z")  # day 1, already crashed post-insert

    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    # day 2's run: v1 (created day 1) is nowhere in this listing at all --
    # if range-independent resume didn't exist, this row would never be
    # touched again.
    fake_twitch.queue("/videos", FakeResponse(200, {"data": [], "pagination": {}}))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})  # publish() readback
    fake_youtube.queue("videos", "update", {"id": "yt1"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})  # verify_privacy

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-02", "2026-07-02",  # day 2 -- does NOT include v1's day-1 date
    ], capsys)

    assert code == exitcodes.OK, out
    assert out["videos"][0]["twitch_vod_id"] == "v1"
    assert out["videos"][0]["status"] == "uploaded"
    assert not any(c["resource"] == "videos" and c["method"] == "insert" for c in fake_youtube.calls)

    ledger_path = resolve_ledger_path(vodp_home, "UCabc", register=False)
    ledger = Ledger(ledger_path, channel_id="UCabc")
    assert ledger.get_current("v1")["stage"] == "completed"


def test_upload_f1_crash_at_uploaded_stage_resumes_across_a_range_boundary(
    vodp_home, fake_ffprobe, fake_twitch, fake_youtube, capsys, monkeypatch,
):
    """Same F1 AC as above, for the earlier ``uploaded`` stage (insert
    succeeded, but the post-insert uploadStatus readback never
    completed)."""
    monkeypatch.setattr("vodp.infra.youtube.time.sleep", lambda s: None)
    _seed_env_and_token(vodp_home)
    ledger_path = resolve_ledger_path(vodp_home, "UCabc")
    ledger = Ledger(ledger_path, channel_id="UCabc")
    intent = _fresh_intent_for("v1", "2026-07-01T10:00:00Z")
    ledger.reserve(twitch_vod_id="v1", twitch_user_id="999", profile="default", intent=intent)
    ledger.advance(twitch_vod_id="v1", stage="insert_inflight", expected_stage="reserved")
    ledger.advance(twitch_vod_id="v1", stage="uploaded", youtube_video_id="yt1", expected_stage="insert_inflight")

    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {"data": [], "pagination": {}}))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"uploadStatus": "processed"}}]})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})
    fake_youtube.queue("videos", "update", {"id": "yt1"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-02", "2026-07-02",
    ], capsys)

    assert code == exitcodes.OK, out
    assert out["videos"][0]["status"] == "uploaded"
    assert not any(c["resource"] == "videos" and c["method"] == "insert" for c in fake_youtube.calls)
    ledger2 = Ledger(ledger_path, channel_id="UCabc")
    assert ledger2.get_current("v1")["stage"] == "completed"


def test_resolve_reinsert_refused_when_recovery_happened_post_insert(vodp_home):
    """A recovery_required row reached via a *post-insert* stage (e.g. the
    video is already confirmed uploaded) must never be rewound to
    'reserved' -- that would risk a second insert (Codex critical #3)."""
    from vodp.infra.ledger import Ledger, LedgerError

    ledger_path = resolve_ledger_path(vodp_home, "UCabc")
    ledger = Ledger(ledger_path, channel_id="UCabc")
    intent = {"title": "t", "description": "d", "tags": [], "category_id": "22", "privacy": "private"}
    ledger.reserve(twitch_vod_id="v1", twitch_user_id="u1", profile="default", intent=intent)
    ledger.advance(twitch_vod_id="v1", stage="insert_inflight", expected_stage="reserved")
    ledger.advance(twitch_vod_id="v1", stage="uploaded", youtube_video_id="yt1", expected_stage="insert_inflight")
    ledger.advance(twitch_vod_id="v1", stage="playlist_add_inflight")
    ledger.advance(twitch_vod_id="v1", stage="recovery_required", expected_stage="playlist_add_inflight")

    with pytest.raises(LedgerError):
        ledger.resolve(twitch_vod_id="v1", action="reinsert")
    ledger.resolve(twitch_vod_id="v1", action="abandon")  # still allowed
    assert ledger.get_current("v1")["stage"] == "rejected"


def test_upload_privacy_lock_makes_the_whole_run_exit_6(
    vodp_home, fake_ffprobe, fake_twitch, fake_youtube, capsys, monkeypatch,
):
    """M4 AC: a single failed video (privacy_lock) makes the whole `vods
    upload` run exit 6, not 0, even though nothing else went wrong."""
    monkeypatch.setattr("vodp.infra.youtube.time.sleep", lambda s: None)
    _seed_env_and_token(vodp_home)
    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [_twitch_video("v1", "2026-07-01T10:00:00Z")], "pagination": {},
    }))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    fake_youtube.queue("videos", "insert", {"id": "yt1"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"uploadStatus": "processed"}}]})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})  # publish() read
    fake_youtube.queue("videos", "update", {"id": "yt1"})
    for _ in range(5):
        fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})  # never flips

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01", "--privacy", "public",
    ], capsys)

    assert code == exitcodes.PARTIAL_FAILURE
    assert out["videos"][0]["status"] == "failed"
    assert out["videos"][0]["reason_code"] == "privacy_lock"
    assert out["summary"]["failed"] == 1


def _seed_playlist_add_inflight_row(vodp_home, channel_id, twitch_vod_id, playlist_id="PL1"):
    ledger_path = resolve_ledger_path(vodp_home, channel_id)
    ledger = Ledger(ledger_path, channel_id=channel_id)
    intent = {
        "title": f"Stream {twitch_vod_id}", "description": f"desc\n[vodp:vod={twitch_vod_id}]",
        "tags": [], "category_id": "22", "privacy": "private", "playlist_id": playlist_id,
    }
    ledger.reserve(twitch_vod_id=twitch_vod_id, twitch_user_id="999", profile="default", intent=intent)
    ledger.advance(twitch_vod_id=twitch_vod_id, stage="insert_inflight", expected_stage="reserved")
    ledger.advance(twitch_vod_id=twitch_vod_id, stage="uploaded", youtube_video_id="yt1", expected_stage="insert_inflight")
    ledger.advance(twitch_vod_id=twitch_vod_id, stage="processed", expected_stage="uploaded")
    ledger.advance(twitch_vod_id=twitch_vod_id, stage="meta_verified", expected_stage="processed")
    ledger.advance(twitch_vod_id=twitch_vod_id, stage="playlist_add_inflight", expected_stage="meta_verified")
    return ledger


def _seed_playlist_pseudo_row(vodp_home, channel_id, playlist_name, stage):
    ledger_path = resolve_ledger_path(vodp_home, channel_id)
    ledger = Ledger(ledger_path, channel_id=channel_id)
    pseudo_id = f"__playlist__:{playlist_name}"
    ledger.reserve(twitch_vod_id=pseudo_id, twitch_user_id="__system__", profile="__system__",
                   intent={"kind": "playlist", "playlist_name": playlist_name})
    if stage != "reserved":
        ledger.advance(twitch_vod_id=pseudo_id, stage=stage, expected_stage="reserved")
    return ledger


def test_upload_resumes_playlist_add_inflight_confirmed_completes(
    vodp_home, fake_twitch, fake_youtube, capsys,
):
    """Codex round-3 critical #1: resuming playlist_add_inflight must
    never call playlistItems.insert again -- confirm via listing first."""
    _seed_env_and_token(vodp_home)
    _seed_playlist_add_inflight_row(vodp_home, "UCabc", "v1")

    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {"data": [], "pagination": {}}))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    fake_youtube.queue("playlistItems", "list", {"items": [{"contentDetails": {"videoId": "yt1"}}]})

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-02", "2026-07-02",
    ], capsys)

    assert code == exitcodes.OK, out
    assert out["videos"][0]["status"] == "uploaded"
    assert not any(c["resource"] == "playlistItems" and c["method"] == "insert" for c in fake_youtube.calls)
    ledger_path = resolve_ledger_path(vodp_home, "UCabc", register=False)
    assert Ledger(ledger_path, channel_id="UCabc").get_current("v1")["stage"] == "completed"


def test_upload_resumes_playlist_add_inflight_not_confirmed_is_recovery_required(
    vodp_home, fake_twitch, fake_youtube, capsys,
):
    """The ambiguous case: membership can't be confirmed -- must NOT
    blindly call playlistItems.insert again."""
    _seed_env_and_token(vodp_home)
    _seed_playlist_add_inflight_row(vodp_home, "UCabc", "v1")

    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {"data": [], "pagination": {}}))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    fake_youtube.queue("playlistItems", "list", {"items": []})  # membership not confirmed

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-02", "2026-07-02",
    ], capsys)

    assert code == exitcodes.ENV_OR_API_ERROR
    assert out["videos"][0]["reason_code"] == "recovery_required"
    assert not any(c["resource"] == "playlistItems" and c["method"] == "insert" for c in fake_youtube.calls)


def test_upload_playlist_pseudo_row_create_inflight_not_confirmed_is_recovery_required(
    vodp_home, fake_twitch, fake_youtube, capsys,
):
    """M22: a stuck playlist-creation write-ahead row (response lost)
    must never trigger a second playlists.insert call."""
    _seed_env_and_token(vodp_home)
    _seed_playlist_pseudo_row(vodp_home, "UCabc", "Archive", stage="playlist_create_inflight")

    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [_twitch_video("v1", "2026-07-01T10:00:00Z")], "pagination": {},
    }))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    fake_youtube.queue("playlists", "list", {"items": []})  # no playlist found at all

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01", "--playlist", "Archive",
    ], capsys)

    assert code == exitcodes.ENV_OR_API_ERROR
    assert not any(c["resource"] == "playlists" and c["method"] == "insert" for c in fake_youtube.calls)
    assert not any(c["resource"] == "videos" and c["method"] == "insert" for c in fake_youtube.calls)


def test_upload_playlist_resolved_once_and_cached_across_videos(
    vodp_home, fake_ffprobe, fake_twitch, fake_youtube, capsys, monkeypatch,
):
    """M22: playlist resolved before insert, once per run -- a second
    video reuses it without a second playlists.list call needed at all
    (it's cached via the pseudo ledger row before either video inserts)."""
    monkeypatch.setattr("vodp.infra.youtube.time.sleep", lambda s: None)
    _seed_env_and_token(vodp_home)
    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [
            _twitch_video("v2", "2026-07-01T12:00:00Z"),
            _twitch_video("v1", "2026-07-01T10:00:00Z"),
        ],
        "pagination": {},
    }))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    fake_youtube.queue("playlists", "list", {"items": [{"id": "PL1", "snippet": {"title": "Archive"}}]})
    for vid in ("yt-v2", "yt-v1"):
        fake_youtube.queue("videos", "insert", {"id": vid})
        fake_youtube.queue("videos", "list", {"items": [{"status": {"uploadStatus": "processed"}}]})
        fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})
        fake_youtube.queue("videos", "update", {"id": vid})
        fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})
        fake_youtube.queue("playlistItems", "list", {"items": []})
        fake_youtube.queue("playlistItems", "insert", {"id": f"pli-{vid}"})

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01", "--playlist", "Archive",
    ], capsys)

    assert code == exitcodes.OK, out
    assert all(v["status"] == "uploaded" for v in out["videos"])
    playlist_list_calls = [c for c in fake_youtube.calls if c["resource"] == "playlists" and c["method"] == "list"]
    assert len(playlist_list_calls) == 1  # resolved once, reused for both videos
    insert_calls = [c for c in fake_youtube.calls if c["resource"] == "videos" and c["method"] == "insert"]
    for call in insert_calls:
        assert "プレイリスト: https://www.youtube.com/playlist?list=PL1" in call["kwargs"]["body"]["snippet"]["description"]


def test_upload_orphan_playlist_prevention_exits_2(
    vodp_home, fake_twitch, fake_youtube, capsys,
):
    """M22: creating a brand new playlist is refused (exit 2) when the
    description is already too close to the 5000-byte limit to safely
    absorb an unknown-length playlist URL line."""
    _seed_env_and_token(vodp_home)
    (vodp_home / "profiles" / "default.toml").write_text(
        """
        [meta]
        title_template = "{title}"
        description_template = \"\"\"{title}\n\n""" + ("x" * 4970) + """\"\"\"
        tags = []
        category_id = "22"

        [limits]
        earliest_date = "2000-01-01"
        """,
        encoding="utf-8",
    )
    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [_twitch_video("v1", "2026-07-01T10:00:00Z")], "pagination": {},
    }))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    fake_youtube.queue("playlists", "list", {"items": []})  # needs creating

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01", "--playlist", "Archive",
    ], capsys)

    assert code == exitcodes.ARG_ERROR
    assert not any(c["resource"] == "playlists" and c["method"] == "insert" for c in fake_youtube.calls)


def test_upload_playlist_id_skips_lookup_and_creation(
    vodp_home, fake_ffprobe, fake_twitch, fake_youtube, capsys, monkeypatch,
):
    """--playlist-id bypasses name lookup/creation entirely (M22's
    escape hatch for the orphan-prevention refusal)."""
    monkeypatch.setattr("vodp.infra.youtube.time.sleep", lambda s: None)
    _seed_env_and_token(vodp_home)
    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [_twitch_video("v1", "2026-07-01T10:00:00Z")], "pagination": {},
    }))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    fake_youtube.queue("videos", "insert", {"id": "yt1"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"uploadStatus": "processed"}}]})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})
    fake_youtube.queue("videos", "update", {"id": "yt1"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})
    fake_youtube.queue("playlistItems", "list", {"items": []})
    fake_youtube.queue("playlistItems", "insert", {"id": "pli1"})

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01", "--playlist-id", "PLexisting",
    ], capsys)

    assert code == exitcodes.OK, out
    assert not any(c["resource"] == "playlists" for c in fake_youtube.calls)  # no lookup, no create
    insert_call = [c for c in fake_youtube.calls if c["resource"] == "videos" and c["method"] == "insert"][0]
    assert "プレイリスト: https://www.youtube.com/playlist?list=PLexisting" in insert_call["kwargs"]["body"]["snippet"]["description"]


def test_upload_dry_run_previews_playlist_url_when_found(
    vodp_home, fake_twitch, fake_youtube, capsys,
):
    """M22 dry-run AC: playlists.list only, playlist_url reported when found."""
    _seed_env_and_token(vodp_home)
    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [_twitch_video("v1", "2026-07-01T10:00:00Z")], "pagination": {},
    }))
    fake_youtube.queue("playlists", "list", {"items": [{"id": "PL1", "snippet": {"title": "Archive"}}]})

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01", "--playlist", "Archive", "--dry-run",
    ], capsys)

    assert code == exitcodes.OK
    assert out["videos"][0]["playlist_url"] == "https://www.youtube.com/playlist?list=PL1"
    assert out["videos"][0]["planned_playlist_title"] is None
    assert not any(c["method"] in ("insert", "update") for c in fake_youtube.calls)
    assert not (vodp_home / "ledger").exists()


def test_upload_dry_run_previews_planned_playlist_title_when_not_found(
    vodp_home, fake_twitch, fake_youtube, capsys,
):
    _seed_env_and_token(vodp_home)
    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [_twitch_video("v1", "2026-07-01T10:00:00Z")], "pagination": {},
    }))
    fake_youtube.queue("playlists", "list", {"items": []})

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01", "--playlist", "New Playlist", "--dry-run",
    ], capsys)

    assert code == exitcodes.OK
    assert out["videos"][0]["playlist_url"] is None
    assert out["videos"][0]["planned_playlist_title"] == "New Playlist"
    assert not any(c["method"] in ("insert", "update") for c in fake_youtube.calls)


def test_upload_recovery_required_blocks_new_discovery_and_inserts(
    vodp_home, fake_twitch, fake_youtube, capsys,
):
    """Codex round-3 major #3: an unresolved recovery_required row must
    block *new* inserts for this channel entirely, not just get reported
    alongside them."""
    _seed_env_and_token(vodp_home)
    _seed_insert_inflight_row(vodp_home, "UCabc", "v1")

    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    # a brand new VOD "v2" is available in range -- must NOT be touched.
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [_twitch_video("v2", "2026-07-02T10:00:00Z")], "pagination": {},
    }))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    fake_youtube.queue("channels", "list", {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]})
    fake_youtube.queue("playlistItems", "list", {"items": []})  # v1 footer search: nothing found

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-02", "2026-07-02",
    ], capsys)

    assert code == exitcodes.ENV_OR_API_ERROR
    assert len(out["videos"]) == 1  # only v1's recovery_required row -- v2 never touched
    assert out["videos"][0]["twitch_vod_id"] == "v1"
    assert not any(c["resource"] == "videos" and c["method"] == "insert" for c in fake_youtube.calls)


def test_upload_max_uploads_not_consumed_by_too_long_video(
    vodp_home, fake_ffprobe, fake_twitch, fake_youtube, capsys, monkeypatch,
):
    """Codex round-3 major #5: a video that never reaches the insert call
    (too_long) must not consume the --max-uploads budget."""
    monkeypatch.setattr("vodp.infra.youtube.time.sleep", lambda s: None)
    _seed_env_and_token(vodp_home)
    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [
            _twitch_video("v_long", "2026-07-01T12:00:00Z", duration="13h0m0s"),
            _twitch_video("v_ok", "2026-07-01T10:00:00Z"),
        ],
        "pagination": {},
    }))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    fake_youtube.queue("videos", "insert", {"id": "yt-ok"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"uploadStatus": "processed"}}]})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})
    fake_youtube.queue("videos", "update", {"id": "yt-ok"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01", "--max-uploads", "1",
    ], capsys)
    statuses = {v["twitch_vod_id"]: v["status"] for v in out["videos"]}
    assert statuses["v_long"] == "skipped"
    assert statuses["v_ok"] == "uploaded"  # budget was NOT consumed by v_long


def test_upload_description_file_overrides_template(
    vodp_home, fake_ffprobe, fake_twitch, fake_youtube, capsys, monkeypatch, tmp_path,
):
    """Codex round-3 major #7: --description-file must actually reach the
    uploaded description, not be silently ignored."""
    monkeypatch.setattr("vodp.infra.youtube.time.sleep", lambda s: None)
    _seed_env_and_token(vodp_home)
    desc_file = tmp_path / "desc.txt"
    desc_file.write_text("custom description body", encoding="utf-8")

    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [_twitch_video("v1", "2026-07-01T10:00:00Z")], "pagination": {},
    }))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    fake_youtube.queue("videos", "insert", {"id": "yt1"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"uploadStatus": "processed"}}]})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})
    fake_youtube.queue("videos", "update", {"id": "yt1"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01", "--description-file", str(desc_file),
    ], capsys)
    assert code == exitcodes.OK, out
    insert_call = [c for c in fake_youtube.calls if c["resource"] == "videos" and c["method"] == "insert"][0]
    assert "custom description body" in insert_call["kwargs"]["body"]["snippet"]["description"]


def test_upload_one_video_preflight_failure_does_not_abort_others(
    vodp_home, fake_ffprobe, fake_twitch, fake_youtube, capsys, monkeypatch,
):
    """Codex round-3 major #10: one VOD's failure must not prevent other
    VODs in the same run from being processed."""
    monkeypatch.setattr("vodp.infra.youtube.time.sleep", lambda s: None)
    _seed_env_and_token(vodp_home)
    bad_title = "x" * 101  # over the 100-char preflight limit
    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [
            {"id": "v_bad", "title": bad_title, "created_at": "2026-07-01T12:00:00Z",
             "duration": "10m0s", "url": "https://twitch.tv/videos/v_bad"},
            _twitch_video("v_ok", "2026-07-01T10:00:00Z"),
        ],
        "pagination": {},
    }))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    fake_youtube.queue("videos", "insert", {"id": "yt-ok"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"uploadStatus": "processed"}}]})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})
    fake_youtube.queue("videos", "update", {"id": "yt-ok"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01",
    ], capsys)
    statuses = {v["twitch_vod_id"]: v["status"] for v in out["videos"]}
    assert statuses["v_bad"] == "failed"
    assert statuses["v_ok"] == "uploaded"
    assert code == exitcodes.PARTIAL_FAILURE


def test_upload_mixed_env_and_business_failures_exit_5_not_6(
    vodp_home, fake_ffprobe, fake_twitch, fake_youtube, capsys, monkeypatch,
):
    """M4 AC (Codex/tester review round 2, R3): when one video fails for an
    ordinary business reason (untagged -> 6, "partial failure") *and*
    another fails because of a real environment/API error (an HttpError
    surviving readback, tagged 5) in the same run, exitcodes.combine()'s
    priority (2>4>5>6>3>0) must make the whole run exit 5, not 6 -- a flat
    "any video failed -> 6" aggregation would silently hide the higher-
    priority API failure."""
    from googleapiclient.errors import HttpError

    monkeypatch.setattr("vodp.infra.youtube.time.sleep", lambda s: None)
    _seed_env_and_token(vodp_home)
    bad_title = "x" * 101  # over the 100-char preflight limit -> ordinary "failed" (untagged, default 6)
    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [
            {"id": "v_bad", "title": bad_title, "created_at": "2026-07-01T09:00:00Z",
             "duration": "10m0s", "url": "https://twitch.tv/videos/v_bad"},
            _twitch_video("v_ok", "2026-07-01T10:00:00Z"),
            _twitch_video("v_apierr", "2026-07-01T11:00:00Z"),
        ],
        "pagination": {},
    }))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    # v_ok: an ordinary full happy path.
    fake_youtube.queue("videos", "insert", {"id": "yt-ok"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"uploadStatus": "processed"}}]})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})
    fake_youtube.queue("videos", "update", {"id": "yt-ok"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})
    # v_apierr: insert succeeds, but the readback that follows hits a
    # non-retryable HttpError (404) -- converted to ApiError by
    # infra/youtube.py's _execute(), which _safe_result tags exit_priority
    # ENV_OR_API_ERROR (5), not the generic "failed" default (6).
    fake_youtube.queue("videos", "insert", {"id": "yt-apierr"})
    fake_youtube.queue("videos", "list", HttpError(
        resp=type("R", (), {"status": 404, "reason": "Not Found"})(), content=b"gone",
    ))

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01",
    ], capsys)

    statuses = {v["twitch_vod_id"]: v["status"] for v in out["videos"]}
    assert statuses["v_bad"] == "failed"
    assert statuses["v_ok"] == "uploaded"
    assert statuses["v_apierr"] == "failed"
    assert code == exitcodes.ENV_OR_API_ERROR, (
        f"expected 5 (env/API error outranks plain partial-failure 6), got {code}"
    )


def test_upload_mid_run_401_outranks_apierror_and_business_failure_exit_4(
    vodp_home, fake_ffprobe, fake_twitch, fake_youtube, capsys, monkeypatch,
):
    """R8/M6 state I (Codex/tester review round 2): a video whose post-
    insert readback hits a live HTTP 401 (credentials rejected mid-run,
    design.md §5 state I) must make the *whole run* exit 4 -- outranking
    even another video's ApiError(5) and a third video's plain business
    failure(6) in the same run, per M4's 2>4>5>6>3>0."""
    from googleapiclient.errors import HttpError

    monkeypatch.setattr("vodp.infra.youtube.time.sleep", lambda s: None)
    _seed_env_and_token(vodp_home)
    bad_title = "x" * 101
    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [
            {"id": "v_bad", "title": bad_title, "created_at": "2026-07-01T09:00:00Z",
             "duration": "10m0s", "url": "https://twitch.tv/videos/v_bad"},
            _twitch_video("v_apierr", "2026-07-01T10:00:00Z"),
            _twitch_video("v_401", "2026-07-01T11:00:00Z"),
        ],
        "pagination": {},
    }))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    # v_apierr: a plain non-retryable API failure (5).
    fake_youtube.queue("videos", "insert", {"id": "yt-apierr"})
    fake_youtube.queue("videos", "list", HttpError(
        resp=type("R", (), {"status": 404, "reason": "Not Found"})(), content=b"gone",
    ))
    # v_401: insert succeeds, then the credentials are rejected mid-run.
    fake_youtube.queue("videos", "insert", {"id": "yt-401"})
    fake_youtube.queue("videos", "list", HttpError(
        resp=type("R", (), {"status": 401, "reason": "Unauthorized"})(), content=b"invalid_grant",
    ))

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01",
    ], capsys)

    statuses = {v["twitch_vod_id"]: v["status"] for v in out["videos"]}
    assert statuses["v_bad"] == "failed"
    assert statuses["v_apierr"] == "failed"
    assert statuses["v_401"] == "failed"
    reasons_by_id = {v["twitch_vod_id"]: v["reason_code"] for v in out["videos"]}
    assert reasons_by_id["v_401"] == "auth_required"
    assert code == exitcodes.AUTH_REQUIRED, f"expected 4 (auth-required outranks 5 and 6), got {code}"


def test_upload_401_on_earlier_video_stops_batch_never_tries_later_ones(
    vodp_home, fake_ffprobe, fake_twitch, fake_youtube, capsys, monkeypatch,
):
    """Codex light review (round-2 follow-up on R8): once the
    credentials are confirmed rejected on one video, every remaining
    candidate in the same batch would fail identically -- the run must
    stop there instead of wasting further insert attempts against
    already-known-bad credentials."""
    from googleapiclient.errors import HttpError

    monkeypatch.setattr("vodp.infra.youtube.time.sleep", lambda s: None)
    _seed_env_and_token(vodp_home)
    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [
            _twitch_video("v_401_first", "2026-07-01T09:00:00Z"),
            _twitch_video("v_never_tried", "2026-07-01T10:00:00Z"),
        ],
        "pagination": {},
    }))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    # v_401_first: insert succeeds, then the credentials are rejected.
    fake_youtube.queue("videos", "insert", {"id": "yt-401"})
    fake_youtube.queue("videos", "list", HttpError(
        resp=type("R", (), {"status": 401, "reason": "Unauthorized"})(), content=b"invalid_grant",
    ))
    # v_never_tried: deliberately no fake responses queued at all -- if
    # the batch didn't stop, this would raise AssertionError("no fake
    # handler queued") instead of quietly succeeding.

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01",
    ], capsys)

    assert code == exitcodes.AUTH_REQUIRED
    statuses = {v["twitch_vod_id"]: v["status"] for v in out["videos"]}
    assert statuses["v_401_first"] == "failed"
    assert "v_never_tried" not in statuses
    insert_calls = [c for c in fake_youtube.calls if c["resource"] == "videos" and c["method"] == "insert"]
    assert len(insert_calls) == 1


def test_upload_retries_transient_twitch_5xx_and_still_succeeds(
    vodp_home, fake_ffprobe, fake_twitch, fake_youtube, capsys, monkeypatch,
):
    """M15: the shared retry budget actually gets used end-to-end -- a
    transient 500 from Twitch's /videos does not fail the whole run."""
    monkeypatch.setattr("vodp.infra.youtube.time.sleep", lambda s: None)
    monkeypatch.setattr("time.sleep", lambda s: None)
    _seed_env_and_token(vodp_home)
    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(500, {"error": "transient"}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [_twitch_video("v1", "2026-07-01T10:00:00Z")], "pagination": {},
    }))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    fake_youtube.queue("videos", "insert", {"id": "yt1"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"uploadStatus": "processed"}}]})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})
    fake_youtube.queue("videos", "update", {"id": "yt1"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01",
    ], capsys)

    assert code == exitcodes.OK, out
    assert out["videos"][0]["status"] == "uploaded"


def test_upload_deadline_exceeded_stops_run_and_exits_5(
    vodp_home, fake_twitch, fake_youtube, capsys, monkeypatch,
):
    """M15 AC: a hang/expired deadline stops the run (exit 5), not a
    silent hang or success."""
    _seed_env_and_token(vodp_home)
    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})

    clock = {"t": 0.0}
    monkeypatch.setattr("time.monotonic", lambda: clock["t"])
    real_list_videos = __import__("vodp.infra.twitch", fromlist=["TwitchClient"]).TwitchClient.list_videos

    def hanging_list_videos(self, *a, **kw):
        clock["t"] += 999999.0  # simulate a hang past the deadline
        return real_list_videos(self, *a, **kw)

    monkeypatch.setattr(
        "vodp.infra.twitch.TwitchClient.list_videos", hanging_list_videos,
    )

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01", "--deadline", "1",
    ], capsys)

    assert code == exitcodes.ENV_OR_API_ERROR


def test_upload_playlist_reresolved_each_run_not_cached_forever(
    vodp_home, fake_ffprobe, fake_twitch, fake_youtube, capsys, monkeypatch,
):
    """Codex review, major #9: a pseudo row already 'completed' from a
    previous run must still be re-confirmed via the API this run, not
    trusted forever -- if the playlist was recreated with a new ID, the
    new ID must be used."""
    monkeypatch.setattr("vodp.infra.youtube.time.sleep", lambda s: None)
    _seed_env_and_token(vodp_home)
    _seed_playlist_pseudo_row(vodp_home, "UCabc", "Archive", stage="completed")
    # Directly mark the pseudo row's stored id as a now-stale value.
    from vodp.infra.ledger import Ledger, resolve_ledger_path
    ledger_path = resolve_ledger_path(vodp_home, "UCabc")
    ledger = Ledger(ledger_path, channel_id="UCabc")
    ledger.advance(twitch_vod_id="__playlist__:Archive", stage="completed", outcome="done",
                   youtube_video_id="PL_OLD", expected_stage="completed")

    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [_twitch_video("v1", "2026-07-01T10:00:00Z")], "pagination": {},
    }))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    # Playlist was recreated under the same name with a NEW id.
    fake_youtube.queue("playlists", "list", {"items": [{"id": "PL_NEW", "snippet": {"title": "Archive"}}]})
    fake_youtube.queue("videos", "insert", {"id": "yt1"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"uploadStatus": "processed"}}]})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})
    fake_youtube.queue("videos", "update", {"id": "yt1"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})
    fake_youtube.queue("playlistItems", "list", {"items": []})
    fake_youtube.queue("playlistItems", "insert", {"id": "pli1"})

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01", "--playlist", "Archive",
    ], capsys)

    assert code == exitcodes.OK, out
    insert_call = [c for c in fake_youtube.calls if c["resource"] == "videos" and c["method"] == "insert"][0]
    assert "list=PL_NEW" in insert_call["kwargs"]["body"]["snippet"]["description"]
    assert "list=PL_OLD" not in insert_call["kwargs"]["body"]["snippet"]["description"]
    playlist_create_calls = [c for c in fake_youtube.calls if c["resource"] == "playlists" and c["method"] == "insert"]
    assert playlist_create_calls == []  # never re-created, just re-confirmed


def test_upload_dry_run_playlist_preview_never_writes_auth_files(
    vodp_home, fake_twitch, fake_youtube, capsys,
):
    """Codex/tester review round 2, R2: --playlist dry-run preview must
    never touch the token file on disk, even if a refresh happens in
    memory."""
    _seed_env_and_token(vodp_home)
    before = token_path(vodp_home).stat().st_mtime_ns
    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [_twitch_video("v1", "2026-07-01T10:00:00Z")], "pagination": {},
    }))
    fake_youtube.queue("playlists", "list", {"items": [{"id": "PL1", "snippet": {"title": "Archive"}}]})

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01", "--playlist", "Archive", "--dry-run",
    ], capsys)

    assert code == exitcodes.OK
    after = token_path(vodp_home).stat().st_mtime_ns
    assert before == after  # never rewritten
    assert not token_path(vodp_home).with_name("token.json.bak").exists()


# -- M19 log integration (R12, Codex/tester review round 2): verified
# against a real ``vods upload`` run's actual log file, not just
# JsonLineLogger's own isolated unit tests (tests/test_logging.py) --
# those never proved the real pipeline ever passed ``secrets=`` in at
# all (it didn't, until this round: run_upload() constructed
# ``JsonLineLogger(ctx.home, ctx.run_id)`` with no secrets, so masking
# was dead code in production even though it worked perfectly in
# isolation). ------------------------------------------------------------

def _capture_logger_instance(monkeypatch):
    """Wraps JsonLineLogger.__init__ so the test can get its hands on the
    *exact* instance a real run_upload() call constructs and populated
    via add_secrets(), instead of only being able to inspect the log file
    it already wrote."""
    import vodp.core.logging as logging_module

    captured = {}
    original_init = logging_module.JsonLineLogger.__init__

    def _wrapped(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        captured["logger"] = self

    monkeypatch.setattr(logging_module.JsonLineLogger, "__init__", _wrapped)
    return captured


def test_upload_wires_real_secrets_into_the_run_logger(
    vodp_home, fake_ffprobe, fake_twitch, fake_youtube, capsys, monkeypatch,
):
    """The real run's logger instance must actually have the Twitch
    client secret and OAuth credentials registered -- confirming the
    wiring itself, not just that masking works when a test hands
    JsonLineLogger a secrets= list directly."""
    monkeypatch.setattr("vodp.infra.youtube.time.sleep", lambda s: None)
    captured = _capture_logger_instance(monkeypatch)
    _seed_env_and_token(vodp_home)
    # A distinct sentinel (not "tok", the YouTube token's own value in
    # _seed_env_and_token) so this specifically proves the *Twitch*
    # bearer access token (F4, Codex/tester review round 2, differed-
    # items follow-up) is registered too, not just piggybacking on the
    # YouTube token happening to already be masked.
    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "twitch-sentinel-access-token"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [_twitch_video("v1", "2026-07-01T10:00:00Z")], "pagination": {},
    }))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    fake_youtube.queue("videos", "insert", {"id": "yt1"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"uploadStatus": "processed"}}]})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})
    fake_youtube.queue("videos", "update", {"id": "yt1"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01",
    ], capsys)
    assert code == exitcodes.OK, out

    logger = captured["logger"]
    assert "csecret" in logger.secrets  # TWITCH_CLIENT_SECRET from _seed_env_and_token
    assert "refresh" in logger.secrets  # refresh_token from the seeded token.json
    assert "tok" in logger.secrets  # YouTube access token from the seeded token.json
    assert "twitch-sentinel-access-token" in logger.secrets  # Twitch bearer access token (F4)

    # And, using that *exact* instance a real run constructed, a message
    # that happens to contain one of those real secrets is still masked
    # in the actual log file on disk.
    logger.log("warning", "post_run_secret_check", detail="token=csecret leaked here")
    logger.log("warning", "post_run_twitch_token_check", detail="bearer twitch-sentinel-access-token leaked here")
    [log_file] = list((vodp_home / "logs").glob("*.log"))
    content = log_file.read_text(encoding="utf-8")
    assert "csecret" not in content
    assert "twitch-sentinel-access-token" not in content
    assert "***" in content


def test_upload_twitch_token_registered_even_when_get_user_id_fails_after_fetching_it(
    vodp_home, fake_twitch, fake_youtube, capsys, monkeypatch,
):
    """Codex light review (round-3 follow-up on F4): TwitchClient mints
    its access token via get_app_token() *inside* get_user_id() (before
    the /users lookup itself) -- if that /users lookup then fails (e.g.
    channel not found), a token already sits in memory that must still
    get registered/masked, not only on get_user_id()'s success path."""
    captured = _capture_logger_instance(monkeypatch)
    _seed_env_and_token(vodp_home)
    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "twitch-sentinel-access-token"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": []}))  # channel not found -> get_user_id() raises

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01",
    ], capsys)

    assert code == exitcodes.ENV_OR_API_ERROR
    logger = captured["logger"]
    assert "twitch-sentinel-access-token" in logger.secrets


def test_upload_secret_masked_in_stderr_and_json_error_not_just_the_log_file(
    vodp_home, fake_twitch, fake_youtube, capsys, monkeypatch,
):
    """M19 AC (requirements.md M19: "sentinel secret が stdout/stderr/
    ログに不出現") -- Codex light review (round-2 follow-up on R12): the
    original fix only ever masked the log *file*; an exception raised
    after secrets were registered still printed its raw message
    (possibly containing one) straight to stderr and into the JSON
    envelope's summary.error. This forces exactly that: a failure
    *outside* the per-video isolation (_safe_result), so it reaches
    main.py's own exception handler, with a message that happens to
    embed the real, already-registered Twitch client secret."""
    _seed_env_and_token(vodp_home)
    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})

    from vodp.core.errors import ApiError

    def boom(*a, **kw):
        raise ApiError("channel lookup failed, token=csecret was rejected")

    monkeypatch.setattr(vods_module.yt_infra, "get_own_channel_id", boom)

    code = main([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01",
    ])
    captured = capsys.readouterr()

    assert code == exitcodes.ENV_OR_API_ERROR
    assert "csecret" not in captured.err
    assert "***" in captured.err
    assert "csecret" not in captured.out
    assert "***" in captured.out


def test_upload_hard_cap_actually_triggers_during_a_real_run(
    vodp_home, fake_ffprobe, fake_twitch, fake_youtube, capsys, monkeypatch,
):
    """The hard cap must actually trip against the real log lines
    run_upload() itself writes (download_start/upload_done), not just a
    synthetic loop of direct .log() calls in isolation."""
    import vodp.core.logging as logging_module

    monkeypatch.setattr("vodp.infra.youtube.time.sleep", lambda s: None)
    monkeypatch.setattr(logging_module, "HARD_CAP_BYTES", 10)  # the very first real line already exceeds this
    _seed_env_and_token(vodp_home)
    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [_twitch_video("v1", "2026-07-01T10:00:00Z")], "pagination": {},
    }))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    fake_youtube.queue("videos", "insert", {"id": "yt1"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"uploadStatus": "processed"}}]})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})
    fake_youtube.queue("videos", "update", {"id": "yt1"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01",
    ], capsys)
    assert code == exitcodes.OK, out

    [log_file] = list((vodp_home / "logs").glob("*.log"))
    lines = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    assert any(rec.get("event") == "log_cap_reached" for rec in lines)
    # once capped, nothing past the marker gets appended -- the marker is
    # always the last line.
    assert lines[-1]["event"] == "log_cap_reached"


# -- M15 deadline propagation integration (R12, Codex/tester review round
# 2): the already-implemented parts (ffprobe subprocess timeout, insert
# chunk-boundary check) previously only had unit-level coverage
# (test_ffprobe.py / test_youtube.py calling the infra functions
# directly) -- these exercise the same behavior through the real `vods
# upload` CLI pipeline instead. ------------------------------------------

def test_upload_download_deadline_expired_mid_download_stops_real_run_exit_5(
    vodp_home, fake_ffprobe, fake_twitch, fake_youtube, capsys, monkeypatch,
):
    """F6 (Codex/tester review round 2, differed-items follow-up): the
    deadline check wired into download_vod()'s progress_hook must
    actually stop a real `vods upload` run (exit 5) when it expires
    mid-download, exercised through the full CLI pipeline (not just
    test_downloader.py's unit-level coverage of download_vod() itself).

    Mutation-tested (team-lead follow-up, round 3): the deadline must
    still be *unexpired* going into the download step -- the previous
    version of this test burned the whole deadline during Twitch's
    ``list_videos`` (well before `download_vod()` even ran), so it was
    really `download_vod()`'s own ``timeout_for()`` call that raised, not
    the progress_hook's ``deadline.check()`` this test claims to cover
    (silently "passed" even with the hook's check() neutered). Also
    asserts ffprobe's actual probing call is never reached, so this
    can't be quietly satisfied by the *separate*, already-covered
    ffprobe-deadline mechanism instead."""
    from vodp.infra.downloader import download_vod as real_download_vod

    monkeypatch.setattr("vodp.infra.youtube.time.sleep", lambda s: None)
    _seed_env_and_token(vodp_home)
    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [_twitch_video("v1", "2026-07-01T10:00:00Z")], "pagination": {},
    }))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    # deliberately no videos/insert response queued -- if the download
    # deadline check didn't stop the run, this would fail with "no fake
    # handler queued" instead of the expected DeadlineExceededError path.

    clock = {"t": 0.0}
    monkeypatch.setattr("time.monotonic", lambda: clock["t"])

    hook_calls = []

    class SlowDownloadYDL:
        """Overrides the module's autouse FakeYDL fixture for just this
        test -- advances the clock past the deadline *inside*
        extract_info, right before the progress_hook call, so the
        hook's own deadline.check() is what raises here, not an
        already-expired deadline caught earlier."""

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
                clock["t"] = 999999.0  # deadline expires mid-transfer, not before the download even starts
                hook({"status": "downloading", "filename": path})  # must raise right here
            return {"requested_downloads": [{"filepath": path}]}

    def slow_download_vod(url, download_dir, vod_id, **kwargs):
        kwargs["ydl_class"] = SlowDownloadYDL
        return real_download_vod(url, download_dir, vod_id, **kwargs)

    monkeypatch.setattr(vods_module.downloader_infra, "download_vod", slow_download_vod)

    # A plain flag, not relying on exit-code coincidence: main.py's own
    # defensive "except Exception" catch-all also maps *any* uncaught
    # exception to exit 5 -- if this probe got called and raised, the
    # run would still (wrongly) "pass" on exit code alone, masking the
    # exact mutation-survival bug this test exists to catch.
    probe_called = []

    def unreachable_probe(*a, **kw):
        probe_called.append(True)
        raise AssertionError(
            "ffprobe's actual duration probe must never be reached -- the download "
            "deadline check should have already stopped the run before this point"
        )

    monkeypatch.setattr(vods_module.ffprobe_infra, "probe_duration_seconds", unreachable_probe)

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01", "--deadline", "1000",
    ], capsys)

    assert code == exitcodes.ENV_OR_API_ERROR
    assert hook_calls == ["downloading"]  # the download hook itself is what stopped this
    assert probe_called == []  # ffprobe's real probe was never reached
    assert not any(c["resource"] == "videos" and c["method"] == "insert" for c in fake_youtube.calls)
    # The strongest discriminator: download_vod() itself must be what
    # raised. cli/commands/vods.py's _process_fresh() only enters its
    # try/finally (which unlinks the downloaded file) *after*
    # download_vod() returns -- if download_vod() raised, that file
    # (written by SlowDownloadYDL before the hook fired) is still
    # sitting on disk, never cleaned up. If instead the *next* deadline
    # check downstream (ffprobe's own, already covered separately) is
    # what actually caught it -- e.g. because this hook's own check() was
    # silently neutered -- the file would already be inside that
    # try/finally and get unlinked anyway, masking the difference from
    # the exit code/probe_called alone.
    assert (vodp_home / "downloads" / "vodp-v1.mp4").exists()


def test_upload_ffprobe_timeout_bounded_by_remaining_deadline(
    vodp_home, fake_ffprobe, fake_twitch, fake_youtube, capsys, monkeypatch,
):
    """`_process_fresh` must pass ffprobe a timeout capped at whatever's
    actually left of the run's deadline (design.md §8), not always the
    full 30s regardless of how much budget already burned elsewhere this
    run."""
    monkeypatch.setattr("vodp.infra.youtube.time.sleep", lambda s: None)
    _seed_env_and_token(vodp_home)
    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [_twitch_video("v1", "2026-07-01T10:00:00Z")], "pagination": {},
    }))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})
    fake_youtube.queue("videos", "insert", {"id": "yt1"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"uploadStatus": "processed"}}]})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})
    fake_youtube.queue("videos", "update", {"id": "yt1"})
    fake_youtube.queue("videos", "list", {"items": [{"status": {"privacyStatus": "private"}}]})

    clock = {"t": 0.0}
    monkeypatch.setattr("time.monotonic", lambda: clock["t"])
    real_list_videos = __import__("vodp.infra.twitch", fromlist=["TwitchClient"]).TwitchClient.list_videos

    def slow_list_videos(self, *a, **kw):
        clock["t"] += 20.0  # burn 20s of the 30s deadline before download/ffprobe even starts
        return real_list_videos(self, *a, **kw)

    monkeypatch.setattr("vodp.infra.twitch.TwitchClient.list_videos", slow_list_videos)

    captured_timeout = {}
    real_probe = vods_module.ffprobe_infra.probe_duration_seconds

    def capturing_probe(ffprobe_path, file_path, *, timeout=30.0, **kw):
        captured_timeout["value"] = timeout
        return real_probe(ffprobe_path, file_path, timeout=timeout, **kw)

    monkeypatch.setattr(vods_module.ffprobe_infra, "probe_duration_seconds", capturing_probe)

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01", "--deadline", "30",
    ], capsys)

    assert code == exitcodes.OK, out
    assert "value" in captured_timeout
    # cap=30.0 in the source, but only ~10s of the 30s deadline was left
    # by the time ffprobe ran -- never the full 30s.
    assert 0 < captured_timeout["value"] <= 10.0, captured_timeout


def test_upload_insert_chunk_boundary_deadline_stops_real_run_exit_5(
    vodp_home, fake_ffprobe, fake_twitch, fake_youtube, capsys, monkeypatch,
):
    """The same chunk-boundary deadline check ``test_youtube.py``'s
    ``test_insert_video_checks_deadline_before_each_chunk`` proves at the
    ``insert_video()`` unit level, exercised through the real `vods
    upload` pipeline: the deadline expiring between two resumable-upload
    chunks must stop the whole run (exit 5), never silently finish the
    upload past the deadline."""
    from tests.fakes import FakeYouTubeRequest

    monkeypatch.setattr("vodp.infra.youtube.time.sleep", lambda s: None)
    _seed_env_and_token(vodp_home)
    fake_twitch.queue("oauth2/token", FakeResponse(200, {"access_token": "tok"}))
    fake_twitch.queue("/users", FakeResponse(200, {"data": [{"id": "999"}]}))
    fake_twitch.queue("/videos", FakeResponse(200, {
        "data": [_twitch_video("v1", "2026-07-01T10:00:00Z")], "pagination": {},
    }))
    fake_youtube.queue("channels", "list", {"items": [{"id": "UCabc"}]})

    clock = {"t": 0.0}
    monkeypatch.setattr("time.monotonic", lambda: clock["t"])

    class SlowChunkRequest(FakeYouTubeRequest):
        calls = 0

        def next_chunk(self, num_retries=None):
            SlowChunkRequest.calls += 1
            if SlowChunkRequest.calls == 1:
                clock["t"] += 999999.0  # deadline expires before the *next* chunk boundary check
                return object(), None  # not done yet
            return None, self.execute()

    class _VideosResourceWithSlowInsert:
        def __init__(self, youtube):
            self._youtube = youtube

        def insert(self, **kw):
            return SlowChunkRequest(self._youtube, "videos", "insert", kw)

        def list(self, **kw):
            return FakeYouTubeRequest(self._youtube, "videos", "list", kw)

        def update(self, **kw):
            return FakeYouTubeRequest(self._youtube, "videos", "update", kw)

    fake_youtube.queue("videos", "insert", {"id": "yt1"})
    monkeypatch.setattr(
        "tests.fakes.FakeYouTube.videos",
        lambda self: _VideosResourceWithSlowInsert(self),
    )

    code, out = _run([
        "vods", "upload", "--home", str(vodp_home), "--non-interactive",
        "--range", "2026-07-01", "2026-07-01", "--deadline", "1",
    ], capsys)

    assert code == exitcodes.ENV_OR_API_ERROR
    assert SlowChunkRequest.calls == 1  # the second chunk was never attempted
