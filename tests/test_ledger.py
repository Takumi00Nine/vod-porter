"""M5 AC (§4): stage-by-stage resume with zero duplicate inserts, corrupt
ledger -> exit-5-mapped error, intent-hash mismatch -> exit-5-mapped error,
registry prevents a second ledger path from splitting one channel, and the
repair/resolve admin flows."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vodp.core.errors import LedgerError
from vodp.infra.ledger import (
    Ledger,
    LedgerBlockedError,
    LedgerCorruptError,
    LedgerIntentMismatchError,
    compute_intent_hash,
    resolve_ledger_path,
)

INTENT = {"privacy": "private", "title": "T", "playlist": "P"}


def test_reserve_new_then_get_current(tmp_path):
    ledger = Ledger(tmp_path / "ch.jsonl", channel_id="UCabc")
    result = ledger.reserve(twitch_vod_id="v1", twitch_user_id="u1", profile="default", intent=INTENT)
    assert result.action == "new"
    current = ledger.get_current("v1")
    assert current["stage"] == "reserved"
    assert current["outcome"] == "pending"
    assert current["intent_hash"] == compute_intent_hash(INTENT)


def test_reserve_twice_same_intent_resumes_without_new_row_duplication(tmp_path):
    ledger = Ledger(tmp_path / "ch.jsonl", channel_id="UCabc")
    ledger.reserve(twitch_vod_id="v1", twitch_user_id="u1", profile="default", intent=INTENT)
    ledger.advance(twitch_vod_id="v1", stage="insert_inflight")
    result = ledger.reserve(twitch_vod_id="v1", twitch_user_id="u1", profile="default", intent=INTENT)
    assert result.action == "resume"
    assert result.record["stage"] == "insert_inflight"


def test_reserve_intent_mismatch_raises(tmp_path):
    ledger = Ledger(tmp_path / "ch.jsonl", channel_id="UCabc")
    ledger.reserve(twitch_vod_id="v1", twitch_user_id="u1", profile="default", intent=INTENT)
    other_intent = dict(INTENT, title="Different Title")
    with pytest.raises(LedgerIntentMismatchError):
        ledger.reserve(twitch_vod_id="v1", twitch_user_id="u1", profile="default", intent=other_intent)


def test_reserve_after_completed_skips(tmp_path):
    ledger = Ledger(tmp_path / "ch.jsonl", channel_id="UCabc")
    ledger.reserve(twitch_vod_id="v1", twitch_user_id="u1", profile="default", intent=INTENT)
    ledger.advance(twitch_vod_id="v1", stage="completed", outcome="done")
    result = ledger.reserve(twitch_vod_id="v1", twitch_user_id="u1", profile="default", intent=INTENT)
    assert result.action == "skip_done"


def test_full_happy_path_stage_progression_no_duplicate_insert(tmp_path):
    """Simulates a crash-and-resume after every single stage; the number of
    times we *would* have called insert must stay exactly 1 (M5 AC)."""
    ledger = Ledger(tmp_path / "ch.jsonl", channel_id="UCabc")
    insert_calls = 0

    def process_once():
        nonlocal insert_calls
        result = ledger.reserve(twitch_vod_id="v1", twitch_user_id="u1", profile="default", intent=INTENT)
        current = ledger.get_current("v1")
        stage = current["stage"]
        if stage in (None, "reserved"):
            ledger.advance(twitch_vod_id="v1", stage="insert_inflight")
            insert_calls += 1
            ledger.advance(twitch_vod_id="v1", stage="uploaded", youtube_video_id="yt1")
            stage = "uploaded"
        if stage == "uploaded":
            ledger.advance(twitch_vod_id="v1", stage="processed")
            stage = "processed"
        if stage == "processed":
            ledger.advance(twitch_vod_id="v1", stage="meta_verified")
            stage = "meta_verified"
        if stage == "meta_verified":
            ledger.advance(twitch_vod_id="v1", stage="playlist_added")
            stage = "playlist_added"
        if stage == "playlist_added":
            ledger.advance(twitch_vod_id="v1", stage="completed", outcome="done")

    # "crash" after every advance and resume from scratch each time
    for _ in range(6):
        process_once()

    final = ledger.get_current("v1")
    assert final["stage"] == "completed"
    assert final["outcome"] == "done"
    assert final["youtube_video_id"] == "yt1"
    assert insert_calls == 1  # never re-inserted despite 6 resumes


def test_insert_inflight_resume_does_not_reinsert(tmp_path):
    ledger = Ledger(tmp_path / "ch.jsonl", channel_id="UCabc")
    ledger.reserve(twitch_vod_id="v1", twitch_user_id="u1", profile="default", intent=INTENT)
    ledger.advance(twitch_vod_id="v1", stage="insert_inflight")
    # crash here (response lost); resume must NOT call insert again --
    # it should instead mark recovery_required per §4.
    current = ledger.get_current("v1")
    assert current["stage"] == "insert_inflight"
    ledger.advance(twitch_vod_id="v1", stage="recovery_required", outcome="retryable_failure")
    current = ledger.get_current("v1")
    assert current["stage"] == "recovery_required"


def test_resumable_entries_lists_inflight_and_recovery(tmp_path):
    ledger = Ledger(tmp_path / "ch.jsonl", channel_id="UCabc")
    ledger.reserve(twitch_vod_id="v1", twitch_user_id="u1", profile="default", intent=INTENT)
    ledger.advance(twitch_vod_id="v1", stage="insert_inflight")
    ledger.reserve(twitch_vod_id="v2", twitch_user_id="u1", profile="default", intent=INTENT)
    ledger.advance(twitch_vod_id="v2", stage="completed", outcome="done")
    entries = ledger.resumable_entries()
    ids = {e["twitch_vod_id"] for e in entries}
    assert ids == {"v1"}


def test_resumable_stages_taxonomy_matches_design_section_4(tmp_path):
    """design.md §4's "再開キュー" (Codex/tester review round 2,
    differed-items follow-up F1): every stage a row can be durably
    parked at *after* ``reserve()`` and *before* ``completed`` is
    range-independently resumable -- insert_inflight /
    playlist_create_inflight / playlist_add_inflight / processing_pending
    / recovery_required / privacy_lock / uploaded / processed /
    meta_verified / playlist_added.

    Originally (R4) the four post-write-ahead stages (uploaded/processed/
    meta_verified/playlist_added) were deliberately excluded on the
    theory that in-range rediscovery (``_process_one_candidate``'s
    ``stage != "reserved"`` branch) was enough -- tester's independent
    verification found the gap that theory missed: a crash on day 1
    followed by an unattended run whose default --range is day 2 would
    never rediscover that VOD again, stranding it forever. Only
    ``reserved`` and ``completed`` are excluded now -- ``reserved`` never
    touched a non-idempotent call (§4 C1, safe to just leave inert), and
    ``completed`` is done."""
    from vodp.infra.ledger import RESUMABLE_STAGES

    assert RESUMABLE_STAGES == {
        "insert_inflight",
        "uploaded",
        "processed",
        "meta_verified",
        "playlist_create_inflight",
        "playlist_add_inflight",
        "playlist_added",
        "processing_pending",
        "recovery_required",
        "privacy_lock",
    }
    assert "reserved" not in RESUMABLE_STAGES
    assert "completed" not in RESUMABLE_STAGES


def test_corrupt_line_raises_ledger_corrupt_error(tmp_path):
    path = tmp_path / "ch.jsonl"
    path.write_text('{"kind": "state", "twitch_vod_id": "v1", "stage": "reserved"}\nnot json\n', encoding="utf-8")
    ledger = Ledger(path, channel_id="UCabc")
    with pytest.raises(LedgerCorruptError):
        ledger.load_all()


def test_corrupt_ledger_is_env_or_api_error_subtype():
    from vodp.core.errors import EnvironmentError_
    assert issubclass(LedgerCorruptError, EnvironmentError_)
    assert issubclass(LedgerCorruptError, LedgerError)


def _state_row(**overrides):
    row = {
        "kind": "state", "twitch_vod_id": "v1", "youtube_channel_id": "UCabc",
        "stage": "reserved", "outcome": "pending",
    }
    row.update(overrides)
    return row


def test_repair_drops_trailing_partial_line_and_unblocks(tmp_path):
    path = tmp_path / "ch.jsonl"
    good = json.dumps(_state_row())
    # No trailing newline after the broken fragment: the torn-write signature.
    path.write_text(good + "\n" + '{"broken', encoding="utf-8")
    ledger = Ledger(path, channel_id="UCabc")
    result = ledger.repair()
    assert result["repaired"] is True
    assert ledger.get_current("v1")["stage"] == "reserved"
    assert not ledger.blocked_marker.exists()


def test_repair_blocks_when_corruption_not_at_tail(tmp_path):
    path = tmp_path / "ch.jsonl"
    good = json.dumps(_state_row())
    path.write_text("not json at all\n" + good + "\n", encoding="utf-8")
    ledger = Ledger(path, channel_id="UCabc")
    result = ledger.repair()
    assert result["repaired"] is False
    assert result["blocked"] is True
    assert ledger.blocked_marker.exists()
    with pytest.raises(LedgerBlockedError):
        ledger.get_current("v1")


def test_resolve_reinsert_resets_to_reserved(tmp_path):
    ledger = Ledger(tmp_path / "ch.jsonl", channel_id="UCabc")
    ledger.reserve(twitch_vod_id="v1", twitch_user_id="u1", profile="default", intent=INTENT)
    ledger.advance(twitch_vod_id="v1", stage="insert_inflight")
    ledger.advance(twitch_vod_id="v1", stage="recovery_required")
    ledger.resolve(twitch_vod_id="v1", action="reinsert")
    assert ledger.get_current("v1")["stage"] == "reserved"


def test_resolve_abandon_marks_permanent_failure(tmp_path):
    ledger = Ledger(tmp_path / "ch.jsonl", channel_id="UCabc")
    ledger.reserve(twitch_vod_id="v1", twitch_user_id="u1", profile="default", intent=INTENT)
    ledger.advance(twitch_vod_id="v1", stage="insert_inflight")
    ledger.advance(twitch_vod_id="v1", stage="recovery_required")
    ledger.resolve(twitch_vod_id="v1", action="abandon")
    current = ledger.get_current("v1")
    assert current["stage"] == "rejected"
    assert current["outcome"] == "permanent_failure"


def test_resolve_requires_recovery_required_stage(tmp_path):
    ledger = Ledger(tmp_path / "ch.jsonl", channel_id="UCabc")
    ledger.reserve(twitch_vod_id="v1", twitch_user_id="u1", profile="default", intent=INTENT)
    with pytest.raises(LedgerError):
        ledger.resolve(twitch_vod_id="v1", action="abandon")


def test_registry_prevents_split_ledger_for_same_channel(tmp_path):
    path_a = resolve_ledger_path(tmp_path, "UCabc")
    with pytest.raises(LedgerError):
        resolve_ledger_path(tmp_path, "UCabc", override=str(tmp_path / "other.jsonl"))
    # same path is fine even if passed again as an override
    resolve_ledger_path(tmp_path, "UCabc", override=str(path_a))


def test_registry_registers_first_use(tmp_path):
    path = resolve_ledger_path(tmp_path, "UCabc")
    registry = json.loads((tmp_path / "ledger" / "registry.json").read_text())
    assert registry["UCabc"] == path.relative_to(tmp_path).as_posix()


# -- VODP_HOME relocation hardening (2026-08-08, team-lead-assigned): -------
# registry stores home-relative paths (so a whole-home move stays valid),
# with automatic one-way migration of old absolute-format entries, and an
# absolute path retained verbatim for anything genuinely outside home.


def test_registry_stores_home_relative_path_for_canonical_default(tmp_path):
    path = resolve_ledger_path(tmp_path, "UCabc")
    registry = json.loads((tmp_path / "ledger" / "registry.json").read_text())
    assert registry["UCabc"] == "ledger/UCabc.jsonl"
    assert path == tmp_path / "ledger" / "UCabc.jsonl"


def test_registry_survives_home_relocation(tmp_path):
    """The exact 2026-07-29 failure mode: VODP_HOME is moved wholesale
    (dir renamed) and a later run must still resolve the same channel to
    its ledger under the *new* home, with no manual sed-style repair."""
    old_home = tmp_path / "old_home"
    resolve_ledger_path(old_home, "UCabc")

    new_home = tmp_path / "new_home"
    old_home.rename(new_home)

    # Must resolve cleanly under the new home, both read-write and
    # read-only, with no LedgerError registry mismatch.
    resolved = resolve_ledger_path(new_home, "UCabc")
    assert resolved == new_home / "ledger" / "UCabc.jsonl"
    resolved_ro = resolve_ledger_path(new_home, "UCabc", register=False)
    assert resolved_ro == resolved


def test_registry_migrates_old_absolute_format_entry_and_is_idempotent(tmp_path):
    """A pre-existing registry written by the old code (absolute paths,
    even for entries under home) is read correctly and rewritten to the
    relative form on the next register=True call -- one-directional,
    lossless (same resolved path before/after), and idempotent (running
    it again doesn't change the file further)."""
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir(parents=True)
    old_style_path = ledger_dir / "UCabc.jsonl"
    registry_path = ledger_dir / "registry.json"
    registry_path.write_text(json.dumps({"UCabc": str(old_style_path)}), encoding="utf-8")

    resolved = resolve_ledger_path(tmp_path, "UCabc")
    assert resolved == old_style_path  # lossless: same target as before migration

    migrated = json.loads(registry_path.read_text())
    assert migrated["UCabc"] == "ledger/UCabc.jsonl"  # migrated to relative

    # idempotent: running again with the now-relative entry changes nothing
    resolve_ledger_path(tmp_path, "UCabc")
    assert json.loads(registry_path.read_text()) == migrated


def test_registry_keeps_out_of_home_override_absolute_and_unmigrated(tmp_path):
    """A --ledger override pointing entirely outside VODP_HOME can't be
    relativized and is stored/left as an absolute path -- moving home
    must not (and structurally cannot) affect it."""
    outside_dir = tmp_path / "elsewhere"
    outside_dir.mkdir()
    outside_path = outside_dir / "custom.jsonl"

    resolve_ledger_path(tmp_path / "home", "UCabc", override=str(outside_path))
    registry_path = tmp_path / "home" / "ledger" / "registry.json"
    registry = json.loads(registry_path.read_text())
    assert registry["UCabc"] == str(outside_path)  # absolute, unrelativizable

    # a subsequent run still resolves it correctly and doesn't rewrite it
    resolved = resolve_ledger_path(tmp_path / "home", "UCabc", override=str(outside_path))
    assert resolved == outside_path
    assert json.loads(registry_path.read_text()) == registry


def test_registry_migration_works_when_home_passed_relative_or_via_symlink(tmp_path, monkeypatch):
    """Codex review major #2: the migration/uniqueness comparisons must
    use the same normalization for ``home`` as for stored values --
    otherwise a relative or symlinked ``home`` makes an under-home old
    absolute entry wrongly look external and never migrates."""
    real_home = tmp_path / "real_home"
    old_style_path = real_home / "ledger" / "UCabc.jsonl"
    (real_home / "ledger").mkdir(parents=True)
    (real_home / "ledger" / "registry.json").write_text(
        json.dumps({"UCabc": str(old_style_path)}), encoding="utf-8"
    )

    # relative home (relative to the test's cwd)
    monkeypatch.chdir(tmp_path)
    resolved = resolve_ledger_path(Path("real_home"), "UCabc")
    assert resolved == old_style_path
    migrated = json.loads((real_home / "ledger" / "registry.json").read_text())
    assert migrated["UCabc"] == "ledger/UCabc.jsonl"

    # symlinked home
    link_home = tmp_path / "link_home"
    link_home.symlink_to(real_home)
    resolved_via_link = resolve_ledger_path(link_home, "UCabc")
    assert resolved_via_link == old_style_path


def test_registry_migration_leaves_out_of_home_symlinked_entry_byte_for_byte(tmp_path):
    """Codex review minor follow-up: an absolute registry entry that's
    genuinely outside home must never be rewritten during migration, not
    even to a `.resolve()`-normalized spelling of the same real path
    (e.g. a symlink hop) -- only entries that actually become relative
    are ever touched."""
    real_external = tmp_path / "real_external"
    real_external.mkdir()
    link_external = tmp_path / "link_external"
    link_external.symlink_to(real_external)
    unresolved_external_entry = str(link_external / "custom.jsonl")
    assert unresolved_external_entry != str(Path(unresolved_external_entry).resolve())  # sanity: symlink differs

    home = tmp_path / "home"
    (home / "ledger").mkdir(parents=True)
    canonical_path = home / "ledger" / "UCabc.jsonl"
    (home / "ledger" / "registry.json").write_text(json.dumps({
        "UCabc": str(canonical_path),  # old-format, under home -- expected to migrate
        "UCexternal": unresolved_external_entry,  # old-format, outside home -- must stay untouched
    }), encoding="utf-8")

    resolve_ledger_path(home, "UCabc")

    registry = json.loads((home / "ledger" / "registry.json").read_text())
    assert registry["UCabc"] == "ledger/UCabc.jsonl"  # migrated
    assert registry["UCexternal"] == unresolved_external_entry  # byte-for-byte unchanged


def test_registry_readonly_lookup_resolves_relative_entry(tmp_path):
    """register=False must resolve a relative-format entry the same way
    the register=True path does, without ever writing anything."""
    resolve_ledger_path(tmp_path, "UCabc")  # establishes the relative entry
    registry_path = tmp_path / "ledger" / "registry.json"
    before = registry_path.read_text()

    resolved = resolve_ledger_path(tmp_path, "UCabc", register=False)
    assert resolved == tmp_path / "ledger" / "UCabc.jsonl"
    assert registry_path.read_text() == before  # still side-effect-free


def test_event_rows_do_not_affect_current_state(tmp_path):
    ledger = Ledger(tmp_path / "ch.jsonl", channel_id="UCabc")
    ledger.reserve(twitch_vod_id="v1", twitch_user_id="u1", profile="default", intent=INTENT)
    ledger.advance(twitch_vod_id="v1", stage="completed", outcome="done")
    ledger.append_event(twitch_vod_id="v1", operation="playlist_membership_added", playlist_id="PL1")
    current = ledger.get_current("v1")
    assert current["stage"] == "completed"  # unaffected by the event row


# -- Codex review round 1 hardening --------------------------------------


def test_cross_channel_row_is_corrupt(tmp_path):
    path = tmp_path / "ch.jsonl"
    other = Ledger(path, channel_id="UCother")
    other.reserve(twitch_vod_id="v1", twitch_user_id="u1", profile="default", intent=INTENT)
    ledger = Ledger(path, channel_id="UCabc")
    with pytest.raises(LedgerCorruptError):
        ledger.get_current("v1")


def test_duplicate_key_in_row_is_corrupt(tmp_path):
    path = tmp_path / "ch.jsonl"
    path.write_text('{"kind": "state", "twitch_vod_id": "v1", "twitch_vod_id": "v2"}\n', encoding="utf-8")
    ledger = Ledger(path, channel_id="UCabc")
    with pytest.raises(LedgerCorruptError):
        ledger.load_all()


def test_missing_required_field_is_corrupt(tmp_path):
    path = tmp_path / "ch.jsonl"
    path.write_text('{"kind": "state", "twitch_vod_id": "v1"}\n', encoding="utf-8")
    ledger = Ledger(path, channel_id="UCabc")
    with pytest.raises(LedgerCorruptError):
        ledger.load_all()


def test_invalid_utf8_is_corrupt(tmp_path):
    path = tmp_path / "ch.jsonl"
    path.write_bytes(b'\xff\xfe not valid utf-8\n')
    ledger = Ledger(path, channel_id="UCabc")
    with pytest.raises(LedgerCorruptError):
        ledger.load_all()


def test_registry_rejects_same_path_for_two_channels(tmp_path):
    path = resolve_ledger_path(tmp_path, "UCabc")
    with pytest.raises(LedgerError):
        resolve_ledger_path(tmp_path, "UCdef", override=str(path))


def test_advance_expected_stage_guard_rejects_stale_transition(tmp_path):
    ledger = Ledger(tmp_path / "ch.jsonl", channel_id="UCabc")
    ledger.reserve(twitch_vod_id="v1", twitch_user_id="u1", profile="default", intent=INTENT)
    ledger.advance(twitch_vod_id="v1", stage="insert_inflight")
    with pytest.raises(LedgerError):
        # stale caller thinks it's still "reserved"
        ledger.advance(twitch_vod_id="v1", stage="insert_inflight", expected_stage="reserved")
    # correct expectation still works
    ledger.advance(twitch_vod_id="v1", stage="uploaded", expected_stage="insert_inflight", youtube_video_id="yt1")
    assert ledger.get_current("v1")["stage"] == "uploaded"


def test_resolve_reinsert_refused_once_video_id_exists(tmp_path):
    ledger = Ledger(tmp_path / "ch.jsonl", channel_id="UCabc")
    ledger.reserve(twitch_vod_id="v1", twitch_user_id="u1", profile="default", intent=INTENT)
    ledger.advance(twitch_vod_id="v1", stage="insert_inflight")
    ledger.advance(twitch_vod_id="v1", stage="uploaded", youtube_video_id="yt1")
    ledger.advance(twitch_vod_id="v1", stage="playlist_add_inflight")
    ledger.advance(twitch_vod_id="v1", stage="recovery_required")
    with pytest.raises(LedgerError):
        ledger.resolve(twitch_vod_id="v1", action="reinsert")
    # abandon is still fine (never re-attempts insert)
    ledger.resolve(twitch_vod_id="v1", action="abandon")
    assert ledger.get_current("v1")["stage"] == "rejected"


def test_append_event_blocked_when_marker_present(tmp_path):
    path = tmp_path / "ch.jsonl"
    ledger = Ledger(path, channel_id="UCabc")
    ledger.reserve(twitch_vod_id="v1", twitch_user_id="u1", profile="default", intent=INTENT)
    ledger.blocked_marker.write_text("blocked\n", encoding="utf-8")
    with pytest.raises(LedgerBlockedError):
        ledger.append_event(twitch_vod_id="v1", operation="whatever")


def test_repair_marker_written_before_original_is_touched_survives_crash(tmp_path):
    """Simulate a crash right after the marker is durably written but
    before the canonical file is rebuilt: the ledger must stay blocked,
    never look like a fresh empty ledger (Codex critical #1)."""
    path = tmp_path / "ch.jsonl"
    good = json.dumps({
        "kind": "state", "twitch_vod_id": "v1", "youtube_channel_id": "UCabc",
        "stage": "reserved", "outcome": "pending",
    })
    path.write_text(good + "\n" + "not json at all\n", encoding="utf-8")  # non-tail corruption
    ledger = Ledger(path, channel_id="UCabc")
    result = ledger.repair()
    assert result["blocked"] is True
    assert ledger.blocked_marker.exists()
    # canonical file was moved aside; a naive re-read must not treat this as an empty, healthy ledger
    with pytest.raises(LedgerBlockedError):
        ledger.get_current("v1")
    with pytest.raises(LedgerBlockedError):
        ledger.reserve(twitch_vod_id="v1", twitch_user_id="u1", profile="default", intent=INTENT)


def test_repair_not_at_tail_when_file_ends_with_newline_stays_blocked(tmp_path):
    """A well-terminated last line that still fails to parse is real
    corruption, not a torn write -- must NOT be auto-dropped (Codex #2)."""
    path = tmp_path / "ch.jsonl"
    good = json.dumps({
        "kind": "state", "twitch_vod_id": "v1", "youtube_channel_id": "UCabc",
        "stage": "reserved", "outcome": "pending",
    })
    path.write_text(good + "\nnot json but newline-terminated\n", encoding="utf-8")
    ledger = Ledger(path, channel_id="UCabc")
    result = ledger.repair()
    assert result["repaired"] is False
    assert result["blocked"] is True


def test_registry_concurrent_first_use_is_race_free(tmp_path):
    """Two threads racing resolve_ledger_path() for the same new channel
    must agree on exactly one canonical path (Codex critical #4)."""
    import threading

    results = []
    errors = []
    barrier = threading.Barrier(4)

    def worker():
        barrier.wait()
        try:
            results.append(resolve_ledger_path(tmp_path, "UCrace"))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(set(str(p) for p in results)) == 1
