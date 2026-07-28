"""``vodp check`` (read-only diagnostic) and ``vodp cache clean`` (orphan
cleanup, default dry-run)."""

from __future__ import annotations

import json
import os
import time

import pytest

from vodp.cli.main import main
from vodp.core import exitcodes


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


def test_check_reports_missing_twitch_env_as_failed_but_still_emits_json(vodp_home, capsys):
    """M4 (Codex/tester review round 2, R3): a missing .env is an
    EnvironmentError_ (5), not a generic partial failure (6) -- each
    check's own natural exit code is preserved via exitcodes.combine()."""
    code, out = _run(["check", "--home", str(vodp_home)], capsys)
    assert code == exitcodes.ENV_OR_API_ERROR
    checks = {v["check"]: v["status"] for v in out["videos"]}
    assert checks["twitch_env"] == "failed"


def test_check_youtube_auth_required_surfaces_as_exit_4_not_6(vodp_home, capsys):
    """A check whose underlying failure is AuthRequiredError (4) must not
    be downgraded to the generic partial-failure code (6)."""
    from vodp.infra.auth import client_secret_path

    (vodp_home / ".env").parent.mkdir(parents=True, exist_ok=True)
    (vodp_home / ".env").write_text(
        "TWITCH_CLIENT_ID=a\nTWITCH_CLIENT_SECRET=b\nTWITCH_CHANNEL_LOGIN=c\n", encoding="utf-8",
    )
    client_secret_path(vodp_home).parent.mkdir(parents=True, exist_ok=True)
    client_secret_path(vodp_home).write_text("{}", encoding="utf-8")

    code, out = _run(["check", "--home", str(vodp_home)], capsys)
    checks = {v["check"]: v["status"] for v in out["videos"]}
    assert checks["youtube_auth"] == "failed"
    # ffprobe may or may not be present on this machine/CI runner, but
    # auth-required (4) must win over any lower-priority ffprobe(5)/ok(0)
    # mix per 2>4>5>6>3>0 -- unless ffprobe genuinely IS missing, in which
    # case 5 correctly wins over 4... no: 4 outranks 5, so 4 always wins.
    assert code == exitcodes.AUTH_REQUIRED


def test_check_is_read_only_no_ledger_or_lock_created(vodp_home, capsys):
    _run(["check", "--home", str(vodp_home)], capsys)
    assert not (vodp_home / "ledger").exists()
    assert not (vodp_home / "run").exists()


def test_check_validates_range_without_fetching(vodp_home, capsys):
    code, out = _run(["check", "--home", str(vodp_home), "--range", "not-a-date", "2026-07-01"], capsys)
    assert code == exitcodes.ARG_ERROR


def test_check_profile_arg_error_propagates_as_exit_2_no_json(vodp_home, capsys):
    """M4/M8 (Codex light review, round-2 follow-up): a profile-level
    ArgError (bad template variable) must abort `check` the exact same
    way it aborts every other command -- exit 2, stderr only, no JSON --
    not get caught into an ordinary "failed" check result and surface
    code 2 through the normal (non-exception) return path, which
    main.py's ARG_ERROR-no-JSON guard does not cover."""
    (vodp_home / "profiles").mkdir(parents=True, exist_ok=True)
    (vodp_home / "profiles" / "bad.toml").write_text(
        '[meta]\ntitle_template = "{not_a_real_var}"\n', encoding="utf-8",
    )
    code, out = _run(["check", "--home", str(vodp_home), "--profile", "bad"], capsys)
    assert code == exitcodes.ARG_ERROR
    assert out is None


def test_check_never_saves_a_refreshed_token(vodp_home, capsys, monkeypatch):
    """M7/M19 follow-up: `check`'s own docstring promises "no token
    save" -- an expired-but-refreshable token must not get a refreshed
    copy persisted to disk as a side effect of `check` alone. Uses a
    faked, always-succeeding ``Credentials.refresh`` (like
    tests/test_auth.py's own state-C tests) so this genuinely exercises
    the save-suppression, not just "the real refresh call never got that
    far because the network is blocked" (which would pass either way,
    dry_run or not, and prove nothing)."""
    import json as _json
    from datetime import datetime, timedelta

    from google.oauth2.credentials import Credentials

    from vodp.infra.auth import SCOPES, token_path

    token_path(vodp_home).parent.mkdir(parents=True, exist_ok=True)
    token_path(vodp_home).write_text(_json.dumps({
        "token": "expired-tok", "refresh_token": "refresh", "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "cid", "client_secret": "csecret", "scopes": list(SCOPES),
        "expiry": (datetime.utcnow() - timedelta(hours=1)).isoformat(),
    }), encoding="utf-8")
    before = token_path(vodp_home).read_text(encoding="utf-8")

    def fake_refresh(self, request):
        self.token = "refreshed-tok"
        self.expiry = datetime.utcnow() + timedelta(hours=1)

    monkeypatch.setattr(Credentials, "refresh", fake_refresh)

    _run(["check", "--home", str(vodp_home)], capsys)

    after = token_path(vodp_home).read_text(encoding="utf-8")
    assert before == after  # the refreshed-in-memory token never reached disk
    assert '"refreshed-tok"' not in after
    assert not token_path(vodp_home).with_name("token.json.bak").exists()


def test_cache_clean_default_dry_run_leaves_files(vodp_home, capsys):
    downloads = vodp_home / "downloads"
    downloads.mkdir(parents=True)
    old_file = downloads / "vodp-v1.mp4"
    old_file.write_bytes(b"x")
    old_time = time.time() - 999999
    os.utime(old_file, (old_time, old_time))

    code, out = _run(["cache", "clean", "--home", str(vodp_home)], capsys)
    assert code == exitcodes.OK
    assert old_file.exists()
    assert out["videos"][0]["reason_code"] == "dry_run"


def test_cache_clean_yes_removes_old_files_only(vodp_home, capsys):
    downloads = vodp_home / "downloads"
    downloads.mkdir(parents=True)
    old_file = downloads / "vodp-v1.mp4"
    old_file.write_bytes(b"x")
    old_time = time.time() - 999999
    os.utime(old_file, (old_time, old_time))
    fresh_file = downloads / "vodp-v2.mp4"
    fresh_file.write_bytes(b"x")

    code, out = _run(["cache", "clean", "--home", str(vodp_home), "--yes"], capsys)
    assert code == exitcodes.OK
    assert not old_file.exists()
    assert fresh_file.exists()


def test_cache_clean_non_interactive_without_yes_exits_2(vodp_home, capsys):
    code, out = _run(["cache", "clean", "--home", str(vodp_home), "--non-interactive"], capsys)
    assert code == exitcodes.ARG_ERROR


def _seed_ledger_row(vodp_home, channel_id, vod_id, stage, outcome="pending"):
    from vodp.infra.ledger import Ledger, resolve_ledger_path

    ledger_path = resolve_ledger_path(vodp_home, channel_id)
    ledger = Ledger(ledger_path, channel_id=channel_id)
    ledger.reserve(twitch_vod_id=vod_id, twitch_user_id="u1", profile="default", intent={"a": 1})
    if stage != "reserved":
        ledger.advance(twitch_vod_id=vod_id, stage=stage, outcome=outcome, expected_stage="reserved")
    return ledger


def _old_file(downloads_dir, name):
    downloads_dir.mkdir(parents=True, exist_ok=True)
    path = downloads_dir / name
    path.write_bytes(b"x")
    old_time = time.time() - 999999
    os.utime(path, (old_time, old_time))
    return path


def test_cache_clean_never_deletes_file_for_active_ledger_stage(vodp_home, capsys):
    """S6: ledger state is the primary criterion -- an old file for a VOD
    still mid-pipeline (e.g. insert_inflight, awaiting recovery) must never
    be deleted regardless of age."""
    _seed_ledger_row(vodp_home, "UCabc", "v1", stage="insert_inflight")
    old_file = _old_file(vodp_home / "downloads", "vodp-v1.mp4")

    code, out = _run(["cache", "clean", "--home", str(vodp_home), "--channel", "UCabc", "--yes"], capsys)
    assert code == exitcodes.OK
    assert old_file.exists()
    assert out["videos"] == []


def test_cache_clean_deletes_old_file_for_completed_vod(vodp_home, capsys):
    """A file whose VOD is already completed (pipeline should have
    cleaned it up itself) is a genuine orphan once old enough."""
    _seed_ledger_row(vodp_home, "UCabc", "v1", stage="completed", outcome="done")
    old_file = _old_file(vodp_home / "downloads", "vodp-v1.mp4")

    code, out = _run(["cache", "clean", "--home", str(vodp_home), "--channel", "UCabc", "--yes"], capsys)
    assert code == exitcodes.OK
    assert not old_file.exists()
    assert out["videos"][0]["twitch_vod_id"] == "v1"


def test_cache_clean_deletes_old_untracked_file_even_with_channel(vodp_home, capsys):
    """A file with no ledger row at all (never tracked, or ledger long
    since rotated) is still a candidate once old enough."""
    old_file = _old_file(vodp_home / "downloads", "vodp-vunknown.mp4")
    code, out = _run(["cache", "clean", "--home", str(vodp_home), "--channel", "UCabc", "--yes"], capsys)
    assert code == exitcodes.OK
    assert not old_file.exists()


def test_cache_clean_c10_completed_but_uncleaned_file_is_idempotent_across_two_runs(vodp_home, capsys):
    """§4 C10: a crash between reaching ``completed`` and the pipeline's
    own cleanup leaves the download file behind; a second `cache clean
    --yes` run (whether because the first run already caught it, or a
    retry after some other interruption) must be a safe no-op, never an
    error, and never touch anything still legitimately in the ledger."""
    _seed_ledger_row(vodp_home, "UCabc", "v1", stage="completed", outcome="done")
    old_file = _old_file(vodp_home / "downloads", "vodp-v1.mp4")

    code1, out1 = _run(["cache", "clean", "--home", str(vodp_home), "--channel", "UCabc", "--yes"], capsys)
    assert code1 == exitcodes.OK
    assert not old_file.exists()
    assert out1["videos"][0]["twitch_vod_id"] == "v1"

    # re-running immediately after (idempotent cleanup, design.md C10):
    # the file is already gone, so this must report nothing to delete --
    # not an error, not a re-deletion, not a ledger re-write.
    code2, out2 = _run(["cache", "clean", "--home", str(vodp_home), "--channel", "UCabc", "--yes"], capsys)
    assert code2 == exitcodes.OK
    assert out2["videos"] == []


def test_cache_clean_corrupt_ledger_fails_closed_deletes_nothing(vodp_home, capsys):
    """R5 misdeletion guard: if the ledger can't be trusted at all (a torn
    write, per §4 C8), cache clean must fail closed -- abort with no
    deletion whatsoever -- rather than treat "can't confirm the stage" as
    "safe to delete"."""
    from vodp.infra.ledger import resolve_ledger_path

    old_file = _old_file(vodp_home / "downloads", "vodp-v1.mp4")
    ledger_path = resolve_ledger_path(vodp_home, "UCabc")
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text('{"kind": "state", "twitch_vod_id": "v1"\n', encoding="utf-8")  # torn/invalid JSON

    code, out = _run(["cache", "clean", "--home", str(vodp_home), "--channel", "UCabc", "--yes"], capsys)
    assert code == exitcodes.ENV_OR_API_ERROR
    assert old_file.exists()


def test_cache_clean_execute_channel_lock_conflict_exits_5(vodp_home, capsys):
    """--yes (real delete) must fail fast, never block, when another run
    holds the channel lock."""
    from vodp.infra.locking import exclusive_lock

    lock_path = vodp_home / "run" / "channels" / "UCabc.lock"
    with exclusive_lock(lock_path):
        code, out = _run(["cache", "clean", "--home", str(vodp_home), "--channel", "UCabc", "--yes"], capsys)
    assert code == exitcodes.ENV_OR_API_ERROR


def test_cache_clean_dry_run_never_blocks_on_contested_lock(vodp_home, capsys):
    """Dry-run must never hang waiting on a live run's exclusive lock --
    it falls back to a lockless snapshot read instead (M7 boundary)."""
    from vodp.infra.locking import exclusive_lock

    lock_path = vodp_home / "run" / "channels" / "UCabc.lock"
    with exclusive_lock(lock_path):
        code, out = _run(["cache", "clean", "--home", str(vodp_home), "--channel", "UCabc"], capsys)
    assert code == exitcodes.OK
