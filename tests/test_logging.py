"""M19 AC: secret never appears in log output; a single run stays within
the hard cap."""

from __future__ import annotations

import json

from vodp.core.logging import JsonLineLogger, NullLogger


def test_log_writes_one_json_object_per_line(vodp_home):
    logger = JsonLineLogger(vodp_home, run_id="run-1")
    logger.log("info", "started", foo="bar")
    logger.log("info", "finished", foo="baz")
    [log_file] = list((vodp_home / "logs").glob("*.log"))
    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    records = [json.loads(line) for line in lines]
    assert records[0]["event"] == "started"
    assert records[0]["run_id"] == "run-1"
    assert records[1]["event"] == "finished"


def test_sentinel_secret_never_appears_in_log(vodp_home):
    secret = "sekrit-token-abc123"
    logger = JsonLineLogger(vodp_home, run_id="run-1", secrets=[secret])
    logger.log("error", "oauth_failed", detail=f"token={secret} was rejected")
    [log_file] = list((vodp_home / "logs").glob("*.log"))
    content = log_file.read_text(encoding="utf-8")
    assert secret not in content
    assert "***" in content


def _marker_line_bytes() -> int:
    """The one-time "log_cap_reached" marker's own (short, fixed-shape)
    size -- the true ceiling a capped file can reach is
    ``HARD_CAP_BYTES + this``, since the marker itself is always written
    unconditionally once capped (see logging.py's own comment)."""
    import json as _json
    from datetime import datetime, timedelta, timezone

    jst = timezone(timedelta(hours=9))
    marker = _json.dumps({
        "ts": datetime.now(jst).isoformat(), "run_id": "run-1",
        "level": "warning", "event": "log_cap_reached",
    })
    return len(marker.encode("utf-8")) + 1


def test_single_run_stays_within_hard_cap(vodp_home, monkeypatch):
    import vodp.core.logging as logging_mod

    monkeypatch.setattr(logging_mod, "HARD_CAP_BYTES", 200)
    logger = JsonLineLogger(vodp_home, run_id="run-1")
    for i in range(500):
        logger.log("info", "progress", i=i, padding="x" * 20)
    [log_file] = list((vodp_home / "logs").glob("*.log"))
    size = log_file.stat().st_size
    # F7 (Codex/tester review round 2, differed-items follow-up): the cap
    # check now accounts for the about-to-be-written *regular* line's own
    # length, not just the size before it -- a caller-supplied line can no
    # longer push the file arbitrarily far past HARD_CAP_BYTES (previously
    # by up to that line's own length, hence the old "generous slack"
    # comment here). Only the fixed-size, one-time cap marker itself may
    # still push it slightly past the cap.
    assert size <= 200 + _marker_line_bytes()


def test_hard_cap_check_accounts_for_the_line_about_to_be_written(vodp_home, monkeypatch):
    """A regular (non-marker) line must never be the one that pushes the
    file over HARD_CAP_BYTES -- the previous "size >= HARD_CAP_BYTES"
    check only looked at the size *before* writing, so a line landing
    right at the boundary could push the file past the cap by that
    line's own length."""
    import json as _json

    import vodp.core.logging as logging_mod

    logger = JsonLineLogger(vodp_home, run_id="run-1")
    logger.log("info", "first")
    [log_file] = list((vodp_home / "logs").glob("*.log"))
    size_after_first_line = log_file.stat().st_size

    # Set the cap to a couple bytes more than one line already took --
    # without the fix, a same-sized second line would still get written
    # (only the *pre-write* size was ever checked), overshooting the cap
    # by that entire second line's length instead of stopping at it.
    monkeypatch.setattr(logging_mod, "HARD_CAP_BYTES", size_after_first_line + 5)
    logger.log("info", "second")

    content = log_file.read_text(encoding="utf-8")
    lines = [_json.loads(line) for line in content.splitlines()]
    assert lines[-1]["event"] == "log_cap_reached"  # "second" never got written
    assert log_file.stat().st_size <= size_after_first_line + 5 + _marker_line_bytes()


def test_a_later_run_never_re_appends_the_cap_marker_the_same_day(vodp_home, monkeypatch):
    """Codex light review (round-3 follow-up on F7): ``_capped`` is a
    per-*instance* flag -- a brand new ``JsonLineLogger`` (a separate,
    later run the same day, appending to the same day's log file) has no
    memory of a previous run already having capped it, and would
    otherwise re-append its own marker on its very first log() call,
    every single time, piling up one marker per run indefinitely."""
    import vodp.core.logging as logging_mod

    monkeypatch.setattr(logging_mod, "HARD_CAP_BYTES", 10)  # already-tiny cap, first run trips it immediately
    first_run = JsonLineLogger(vodp_home, run_id="run-1")
    first_run.log("info", "first_run_event")
    [log_file] = list((vodp_home / "logs").glob("*.log"))
    size_after_first_run = log_file.stat().st_size

    second_run = JsonLineLogger(vodp_home, run_id="run-2")  # a fresh instance -- simulates a later, separate run
    second_run.log("info", "second_run_event")
    second_run.log("info", "second_run_event_2")

    assert log_file.stat().st_size == size_after_first_run  # no second marker appended
    content = log_file.read_text(encoding="utf-8")
    marker_count = sum(1 for line in content.splitlines() if json.loads(line).get("event") == "log_cap_reached")
    assert marker_count == 1


def test_null_logger_creates_nothing(vodp_home):
    logger = NullLogger()
    logger.log("info", "should_not_persist")
    assert not (vodp_home / "logs").exists()


def test_purge_old_removes_files_past_retention(vodp_home):
    logs_dir = vodp_home / "logs"
    logs_dir.mkdir()
    old_file = logs_dir / "2020-01-01.log"
    old_file.write_text("{}\n")
    logger = JsonLineLogger(vodp_home, run_id="run-1")
    logger.purge_old(retention_days=30)
    assert not old_file.exists()
