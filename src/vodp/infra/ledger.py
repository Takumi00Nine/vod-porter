"""The staged ledger: design.md §4 (risk core).

Source of truth is YouTube itself; the ledger is a reservation slip plus a
progress cache. Every mutation is a JSONL append (never rewritten in place)
guarded by fsync, so a crash can only ever lose the *last unflushed* write,
never corrupt an earlier one.

Two kinds of rows share one file:
  * ``kind": "state"`` -- the row that ``current_states()`` folds over per
    ``twitch_vod_id`` (last one wins). This is the stage/outcome machine.
  * ``kind": "event"`` -- an append-only side history (e.g. "added to
    playlist X on <date>") that never overrides the state row.

Non-idempotent calls (videos.insert, playlists.insert, playlistItems.insert)
must ``advance()`` to the matching ``*_inflight`` stage *before* the call
(write-ahead) and only afterwards may they advance further -- callers own
that ordering, this module only appends what it's told.

Hardening notes (Codex review round 1, see final report to the lead for the
full list): every row is schema-validated on read (unknown stage/outcome,
wrong channel, missing id -> corrupt); the "blocked" marker is written
*before* the corrupt file is ever touched so a crash mid-repair still
leaves the ledger blocked instead of silently looking healthy;
``resolve(reinsert)`` refuses to rewind a row that already has a
youtube_video_id (that can only mean the recovery happened at/after
insert, so "restart from reserved" would risk a second insert).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from vodp.core.errors import LedgerError
from vodp.infra import fsops
from vodp.infra.locking import exclusive_lock

JST = timezone(timedelta(hours=9))

STAGES = (
    "reserved",
    "insert_inflight",
    "uploaded",
    "processed",
    "meta_verified",
    "playlist_create_inflight",
    "playlist_add_inflight",
    "playlist_added",
    "completed",
)
# privacy_lock added per Codex review: a failed M11 readback is unresolved
# work and must be picked up by the resume queue like *_inflight rows.
NON_LINEAR_STAGES = ("processing_pending", "rejected", "recovery_required", "privacy_lock", "deleted")
ALL_STAGES = frozenset(STAGES) | frozenset(NON_LINEAR_STAGES)

OUTCOMES = frozenset({"pending", "done", "terminal_skip", "retryable_failure", "permanent_failure"})
TERMINAL_OUTCOMES = frozenset({"done", "terminal_skip", "permanent_failure"})

# Stages that imply an insert (of any of the 3 non-idempotent kinds) may
# already have been *attempted*; resolve(reinsert) must never rewind these
# back to "reserved" once a youtube_video_id exists (Codex critical #3).
POST_INSERT_STAGES = frozenset({
    "uploaded", "processed", "meta_verified",
    "playlist_create_inflight", "playlist_add_inflight", "playlist_added",
    "completed", "privacy_lock",
})

# Rows in these stages are unresolved work: on the next run they must be
# processed before any *new* VOD discovery (§4 "再開キュー").
#
# Includes every post-insert, pre-"completed" stage (uploaded/processed/
# meta_verified/playlist_added), not just the write-ahead "*_inflight"
# ones (Codex/tester review round 2, differed-items follow-up F1): a row
# left at e.g. "processed" only ever resumed before via *in-range*
# rediscovery (_process_one_candidate's ``stage != "reserved"`` branch,
# see cli/commands/vods.py) -- a crash-and-resume across a range boundary
# (e.g. crashed on day 1, the next unattended run's default --range is
# day 2, day 1 never gets rediscovered again) would leave it stuck
# forever with no way back in. The range-independent resume queue must
# reach it regardless of what range a later run happens to use.
RESUMABLE_STAGES = frozenset({
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
})

_UNSET = object()  # sentinel: "leave this field as it was" in advance()
_REQUIRED_STATE_KEYS = ("twitch_vod_id", "youtube_channel_id", "stage", "outcome")


class LedgerCorruptError(LedgerError):
    pass


class LedgerBlockedError(LedgerError):
    pass


class LedgerIntentMismatchError(LedgerError):
    pass


def _now_iso() -> str:
    return datetime.now(JST).isoformat()


def compute_intent_hash(intent: dict) -> str:
    canonical = json.dumps(intent, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key in ledger row: {key!r}")
        result[key] = value
    return result


@dataclass
class ReserveResult:
    action: str  # "new" | "resume" | "skip_done"
    record: dict


class Ledger:
    def __init__(self, path: Path, channel_id: str):
        self.path = Path(path)
        self.channel_id = channel_id

    # -- blocking (repair pending) --------------------------------------
    @property
    def blocked_marker(self) -> Path:
        return self.path.with_name(self.path.name + ".blocked")

    def ensure_not_blocked(self) -> None:
        if self.blocked_marker.exists():
            raise LedgerBlockedError(
                f"ledger {self.path} is blocked pending `vodp ledger repair` "
                f"(see {self.blocked_marker})"
            )

    # -- reading ---------------------------------------------------------
    def _read_raw_lines(self) -> tuple[list[str], bool]:
        """Returns (lines, ends_with_newline). A file that does NOT end
        with a newline is the one reliable signature of a torn trailing
        write (the only way a POSIX append can be interrupted); anything
        else that fails to parse is treated as real corruption, not a
        crash artifact (Codex review #2)."""
        if not self.path.exists():
            return [], True
        raw = self.path.read_bytes()
        if raw == b"":
            return [], True
        ends_with_newline = raw.endswith(b"\n")
        body = raw[:-1] if ends_with_newline else raw
        if body == b"":
            return [], ends_with_newline
        lines = []
        for chunk in body.split(b"\n"):
            try:
                lines.append(chunk.decode("utf-8"))
            except UnicodeDecodeError as e:
                lines.append(_CorruptLine(str(e)))
        return lines, ends_with_newline

    def _validate_state_row(self, rec: dict, line_no: int) -> None:
        if not isinstance(rec, dict):
            raise LedgerCorruptError(f"ledger {self.path} line {line_no}: row is not an object")
        kind = rec.get("kind", "state")
        if kind not in ("state", "event"):
            raise LedgerCorruptError(f"ledger {self.path} line {line_no}: unknown kind {kind!r}")
        if kind != "state":
            return
        for key in _REQUIRED_STATE_KEYS:
            if rec.get(key) in (None, ""):
                raise LedgerCorruptError(f"ledger {self.path} line {line_no}: missing {key!r}")
        if rec["youtube_channel_id"] != self.channel_id:
            raise LedgerCorruptError(
                f"ledger {self.path} line {line_no}: row belongs to channel "
                f"{rec['youtube_channel_id']!r}, not {self.channel_id!r} (cross-channel contamination)"
            )
        if rec["stage"] not in ALL_STAGES:
            raise LedgerCorruptError(f"ledger {self.path} line {line_no}: unknown stage {rec['stage']!r}")
        if rec["outcome"] not in OUTCOMES:
            raise LedgerCorruptError(f"ledger {self.path} line {line_no}: unknown outcome {rec['outcome']!r}")

    def load_all(self) -> list[dict]:
        """Every parsed row, in append order. Raises LedgerCorruptError if
        even one line fails to parse or fails schema validation
        (fail-closed, M5)."""
        self.ensure_not_blocked()
        lines, _ = self._read_raw_lines()
        records = []
        for i, line in enumerate(lines):
            line_no = i + 1
            if isinstance(line, _CorruptLine):
                raise LedgerCorruptError(f"ledger {self.path} corrupt at line {line_no}: {line.reason}")
            try:
                rec = json.loads(line, object_pairs_hook=_no_duplicate_keys)
            except (json.JSONDecodeError, ValueError) as e:
                raise LedgerCorruptError(f"ledger {self.path} corrupt at line {line_no}: {e}") from e
            self._validate_state_row(rec, line_no)
            records.append(rec)
        return records

    def current_states(self) -> dict[str, dict]:
        """Latest state-kind row per twitch_vod_id (event rows excluded)."""
        result: dict[str, dict] = {}
        for rec in self.load_all():
            if rec.get("kind", "state") == "state":
                result[rec["twitch_vod_id"]] = rec
        return result

    def get_current(self, twitch_vod_id: str) -> dict | None:
        return self.current_states().get(twitch_vod_id)

    def resumable_entries(self) -> list[dict]:
        return [r for r in self.current_states().values() if r.get("stage") in RESUMABLE_STAGES]

    # -- writing -----------------------------------------------------------
    def _append(self, record: dict) -> dict:
        self.ensure_not_blocked()  # checked here too: covers append_event() (Codex #6)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        fsops.append_line_fsync(self.path, line)
        return record

    def _base_record(self, twitch_vod_id: str, current: dict | None, **overrides) -> dict:
        current = current or {}
        record = {
            "kind": "state",
            "ts": _now_iso(),
            "twitch_vod_id": twitch_vod_id,
            "twitch_user_id": current.get("twitch_user_id"),
            "youtube_channel_id": self.channel_id,
            "youtube_video_id": current.get("youtube_video_id"),
            "profile": current.get("profile"),
            "stage": current.get("stage"),
            "outcome": current.get("outcome", "pending"),
            "error": current.get("error"),
            "intent": current.get("intent"),
            "intent_hash": current.get("intent_hash"),
        }
        for key, value in overrides.items():
            if value is not _UNSET:
                record[key] = value
        return record

    def reserve(self, *, twitch_vod_id: str, twitch_user_id: str, profile: str, intent: dict) -> ReserveResult:
        """Check-and-append under the caller's channel flock."""
        current = self.get_current(twitch_vod_id)
        intent_hash = compute_intent_hash(intent)
        if current is None:
            record = self._base_record(
                twitch_vod_id, None,
                twitch_user_id=twitch_user_id, profile=profile,
                stage="reserved", outcome="pending",
                intent=intent, intent_hash=intent_hash,
            )
            self._append(record)
            return ReserveResult("new", record)
        if current.get("stage") == "completed" or current.get("outcome") in TERMINAL_OUTCOMES:
            return ReserveResult("skip_done", current)
        if current.get("intent_hash") != intent_hash:
            raise LedgerIntentMismatchError(
                f"{twitch_vod_id}: resume intent differs from the originally reserved intent"
            )
        return ReserveResult("resume", current)

    def advance(
        self,
        *,
        twitch_vod_id: str,
        stage: str,
        outcome=_UNSET,
        youtube_video_id=_UNSET,
        error=_UNSET,
        expected_stage=_UNSET,
        recovery_from_stage: str | None = None,
    ) -> dict:
        """Append a new state row transitioning ``twitch_vod_id`` to
        ``stage``. Any field left as ``_UNSET`` carries over from the
        current row unchanged.

        ``expected_stage`` (optional, str or tuple of str) is a
        compare-and-append guard: if given and the row's current stage
        doesn't match, raises LedgerError instead of silently overwriting
        a stage some other code path already changed (Codex major #8).

        Note: this signature is deliberately a closed set of keyword-only
        fields (no ``**kwargs`` passthrough) after a Codex review found
        that an open ``**extra_fields`` let a caller silently override
        protected columns like ``kind``, turning a write-ahead row into an
        ignored ``event`` row (Codex critical, round 2).
        """
        if stage not in ALL_STAGES:
            raise ValueError(f"unknown ledger stage: {stage!r}")
        current = self.get_current(twitch_vod_id)
        if expected_stage is not _UNSET:
            allowed = (expected_stage,) if isinstance(expected_stage, str) else tuple(expected_stage)
            actual = current.get("stage") if current else None
            if actual not in allowed:
                raise LedgerError(
                    f"{twitch_vod_id}: expected stage in {allowed}, found {actual!r} "
                    "(stale advance() rejected)"
                )
        overrides = dict(stage=stage, outcome=outcome, youtube_video_id=youtube_video_id, error=error)
        if stage == "recovery_required":
            overrides["recovery_from_stage"] = recovery_from_stage or (current or {}).get("stage")
        record = self._base_record(twitch_vod_id, current, **overrides)
        return self._append(record)

    def append_event(self, *, twitch_vod_id: str, operation: str, playlist_id: str | None = None) -> dict:
        """Side-history row (never overrides the state fold). Field set is
        closed for the same reason ``advance()``'s is (Codex critical,
        round 2): an open ``**fields`` could shadow ``kind``/``operation``/
        the channel id."""
        record = {
            "kind": "event",
            "ts": _now_iso(),
            "twitch_vod_id": twitch_vod_id,
            "youtube_channel_id": self.channel_id,
            "operation": operation,
            "playlist_id": playlist_id,
        }
        return self._append(record)

    def resolve(self, *, twitch_vod_id: str, action: str) -> dict:
        """``vodp ledger resolve`` for a recovery_required row (§4)."""
        current = self.get_current(twitch_vod_id)
        if current is None or current.get("stage") != "recovery_required":
            raise LedgerError(f"{twitch_vod_id} is not in recovery_required state")
        if action == "reinsert":
            recovery_from = current.get("recovery_from_stage")
            if current.get("youtube_video_id") or recovery_from in POST_INSERT_STAGES:
                raise LedgerError(
                    f"{twitch_vod_id} recovered from {recovery_from!r} "
                    f"(youtube_video_id={current.get('youtube_video_id')!r}); `reinsert` would "
                    "risk a second insert. Use `abandon`, or resolve manually first."
                )
            return self.advance(
                twitch_vod_id=twitch_vod_id, stage="reserved", outcome="pending",
                expected_stage="recovery_required",
            )
        if action == "abandon":
            return self.advance(
                twitch_vod_id=twitch_vod_id, stage="rejected", outcome="permanent_failure",
                expected_stage="recovery_required",
            )
        raise ValueError(f"unknown resolve action: {action!r}")

    def repair(self) -> dict:
        """Quarantine + (if safe) auto-drop a torn trailing write (C8).

        The blocked marker is written *durably* (fsynced) before the
        original file is ever touched, so a crash between "quarantine" and
        "rebuild the canonical file" still leaves the ledger blocked
        instead of silently looking like a fresh, empty, healthy ledger
        (Codex critical #1).
        """
        if not self.path.exists():
            # A previous repair may have quarantined the canonical file and
            # crashed before rebuilding it; the marker is the only durable
            # record of that, so an absent file must NOT be reported as a
            # healthy "no ledger file" if we're still blocked (Codex #12).
            if self.blocked_marker.exists():
                return {"repaired": False, "blocked": True, "reason": "canonical file missing while blocked"}
            return {"repaired": False, "reason": "no ledger file"}
        lines, ends_with_newline = self._read_raw_lines()
        bad_indexes = []
        for i, line in enumerate(lines):
            if isinstance(line, _CorruptLine):
                bad_indexes.append(i)
                continue
            try:
                rec = json.loads(line, object_pairs_hook=_no_duplicate_keys)
                self._validate_state_row(rec, i + 1)
            except (json.JSONDecodeError, ValueError, LedgerCorruptError):
                # schema-invalid rows count as corrupt too (Codex #11) --
                # otherwise repair() would call a schema-broken-but-
                # syntactically-valid file "not corrupt" and delete the
                # marker, only for the very next load_all() to blow up.
                bad_indexes.append(i)
        if not bad_indexes:
            self.blocked_marker.unlink(missing_ok=True)
            return {"repaired": False, "reason": "not corrupt"}

        stamp = datetime.now(JST).strftime("%Y%m%dT%H%M%S.%f")
        quarantine_path = self.path.with_name(self.path.name + f".corrupt.{stamp}")

        # Write the marker FIRST and fsync it before touching the original
        # file at all -- if we crash right after this, the ledger is still
        # (correctly) blocked, never silently "no ledger file" (critical #1).
        fsops.durable_mkdir(self.blocked_marker.parent)
        fsops.atomic_write_bytes(
            self.blocked_marker,
            (
                f"blocked at {_now_iso()}: repairing {self.path} "
                f"({len(bad_indexes)} bad line(s)); about to quarantine to {quarantine_path}\n"
            ).encode("utf-8"),
        )
        self.path.rename(quarantine_path)

        # Only a torn trailing write (file doesn't end with "\n") is ever
        # auto-fixed; a well-terminated-but-invalid last line is real
        # corruption and stays blocked for manual review (Codex #2).
        if bad_indexes == [len(lines) - 1] and not ends_with_newline:
            good_lines = lines[:-1]
            content = ("\n".join(good_lines) + "\n") if good_lines else ""
            fsops.atomic_write_bytes(self.path, content.encode("utf-8"))
            self.blocked_marker.unlink(missing_ok=True)
            return {"repaired": True, "quarantined": str(quarantine_path), "dropped_lines": 1}

        return {"repaired": False, "quarantined": str(quarantine_path), "blocked": True}


class _CorruptLine(str):
    """Marker for a line that failed UTF-8 decoding; carries the reason so
    load_all()/repair() can report a line number without re-decoding."""

    def __new__(cls, reason: str):
        obj = super().__new__(cls, "")
        obj.reason = reason
        return obj


# -- registry: one canonical ledger path per channel (§4) -------------------
#
# 2026-08-08 (VODP_HOME relocation hardening, team-lead-assigned): a
# registry value is stored **relative to home** whenever the path it
# names actually lives under home (the overwhelming common case -- the
# canonical default always does) so that moving VODP_HOME wholesale (dir
# rename/copy) no longer desyncs the registry against reality and trips
# the fail-closed mismatch guard below (this happened for real on
# 2026-07-29 and was hand-patched with `sed`). A value that is NOT under
# home (an explicit ``--ledger`` override pointing elsewhere on disk) is
# fundamentally home-independent and is stored as an absolute path,
# unchanged by any future home move -- that is the correct behavior for
# it, not a limitation.
#
# Old registries (written before this change) always stored absolute
# paths, including for entries that *do* live under home. Format is
# self-describing (``Path(value).is_absolute()``), so those are read
# correctly too, and are opportunistically migrated to the relative form
# and written back the next time this function runs with ``register=True``
# (see ``_migrate_registry_to_relative`` below) -- one-directional
# (absolute-under-home -> relative only; never the reverse), idempotent
# (a value already in its target form is left untouched), and lossless
# (the resolved path never changes, only its on-disk representation).
# This migration only protects a *future* home move that happens after it
# runs -- an already-relocated, never-migrated old registry still needs a
# manual fix once (or a fresh registry) before this helps it.


def _resolve_registry_entry(home: Path, raw: str) -> Path:
    """A stored registry value is either an absolute path (old-format
    entries, or any entry that is genuinely outside ``home``) or a
    path relative to ``home`` (new-format entries under ``home``)."""
    p = Path(raw)
    return p if p.is_absolute() else (home / p)


def _to_registry_value(home: Path, target: Path) -> str:
    """The inverse of ``_resolve_registry_entry``: store relative-to-home
    when possible, absolute when the target isn't under home at all.

    ``target`` is fully re-resolved here (not just ``expanduser()``'d)
    before the ``relative_to`` comparison -- ``resolve_ledger_path``
    already normalizes ``home`` the same way before calling this, but an
    old-format registry entry being migrated (``_migrate_registry_to_
    relative`` below) may itself contain a relative path, a symlink
    hop, or `.`/`..` segments a caller hand-edited; comparing two
    differently-normalized paths would wrongly conclude an entry that
    *is* under home isn't (Codex review, major #2)."""
    target_resolved = Path(target).expanduser().resolve()
    try:
        return target_resolved.relative_to(home).as_posix()
    except ValueError:
        return str(target_resolved)


def _migrate_registry_to_relative(home: Path, registry: dict[str, str]) -> bool:
    """Mutates ``registry`` in place, rewriting any absolute-format entry
    that resolves under the current ``home`` to the relative form.
    Returns True if anything changed (caller decides whether to persist).

    Only ever touches entries that are currently absolute
    (``Path(raw).is_absolute()``) *and* that ``_to_registry_value``
    actually turns into a relative value -- an already-relative entry is
    left completely untouched, even if re-resolving it happened to
    produce an identical value, so the direction of migration (absolute
    -> relative, never the reverse) stays structurally guaranteed rather
    than merely incidental (Codex review, major #2 follow-up). An
    absolute entry that's genuinely outside ``home`` is also left
    byte-for-byte as-is, even if ``.resolve()`` would have normalized it
    to a different (but equivalent) absolute spelling -- e.g. a symlink
    hop like macOS's ``/var`` -> ``/private/var`` -- since that entry was
    never meant to change at all (Codex review, minor follow-up)."""
    dirty = False
    for channel, raw in list(registry.items()):
        if not Path(raw).is_absolute():
            continue
        new_value = _to_registry_value(home, Path(raw))
        if new_value != raw and not Path(new_value).is_absolute():
            registry[channel] = new_value
            dirty = True
    return dirty


def resolve_ledger_path(
    home: Path, channel_id: str, override: str | None = None, *, register: bool = True
) -> Path:
    """--ledger override is only honoured if it matches (or establishes)
    the registry's canonical path for this channel; a mismatch -- or a
    path already claimed by a *different* channel -- is a hard error so
    two ledgers can never silently split (or merge) for one channel
    (Codex critical #4/#5). The whole read-modify-write is done under a
    registry-level flock so two concurrent first-uses can't race and drop
    each other's registration.
    """
    # Fully resolved (not just Path(home)) so the relative/absolute
    # registry-format comparisons below use the same normalization as
    # every stored/looked-up value, regardless of whether the caller
    # already resolved `home` itself -- a relative or symlinked `home`
    # would otherwise make an under-home path wrongly look external
    # (Codex review, major #2).
    home = Path(home).expanduser().resolve()
    ledger_dir = home / "ledger"
    registry_path = ledger_dir / "registry.json"
    canonical_default = ledger_dir / f"{channel_id}.jsonl"
    target = Path(override).expanduser().resolve() if override else canonical_default

    if not register:
        # Read-only lookup: never create the ledger dir, the registry, or
        # its lock file (Codex #14 -- this path must stay side-effect-free
        # for future dry-run/read-only callers). A concurrent writer is
        # tolerated: worst case this returns a stale-but-still-correct
        # answer, which is fine for a read-only caller. Never migrates
        # (writing back is register=True's job only).
        if not registry_path.exists():
            return target
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        existing = registry.get(channel_id)
        return _resolve_registry_entry(home, existing) if existing is not None else target

    fsops.durable_mkdir(ledger_dir)
    # blocking=True: this is a short read-small-json/write-small-json
    # section, so briefly waiting out a concurrent first-use for a
    # *different* channel is preferable to failing outright.
    with exclusive_lock(ledger_dir / "registry.json.lock", blocking=True):
        registry: dict[str, str] = {}
        if registry_path.exists():
            registry = json.loads(registry_path.read_text(encoding="utf-8"))

        dirty = _migrate_registry_to_relative(home, registry)

        # Validate the *whole* registry's path-uniqueness every time, not
        # just for this channel_id -- otherwise a duplicate written before
        # this check existed would go undetected forever (Codex #13).
        seen: dict[str, str] = {}
        for other_channel, other_value in registry.items():
            resolved = str(_resolve_registry_entry(home, other_value).expanduser().resolve())
            if resolved in seen:
                raise LedgerError(
                    f"registry {registry_path} already has two channels ({seen[resolved]!r} and "
                    f"{other_channel!r}) mapped to the same ledger path {other_value}"
                )
            seen[resolved] = other_channel

        existing = registry.get(channel_id)
        if existing is not None:
            existing_path = _resolve_registry_entry(home, existing)
            if existing_path.expanduser().resolve() != Path(target).expanduser().resolve():
                raise LedgerError(
                    f"channel {channel_id} canonical ledger is {existing_path}, "
                    f"but this run resolved {target} (registry mismatch)"
                )
            if dirty:
                fsops.atomic_write_bytes(
                    registry_path, json.dumps(registry, indent=2, sort_keys=True).encode("utf-8")
                )
            return existing_path

        target_resolved = Path(target).expanduser().resolve()
        for other_channel, other_value in registry.items():
            if _resolve_registry_entry(home, other_value).expanduser().resolve() == target_resolved:
                raise LedgerError(
                    f"ledger path {target} is already registered to channel "
                    f"{other_channel!r}; refusing to also use it for {channel_id!r}"
                )

        registry[channel_id] = _to_registry_value(home, target)
        fsops.atomic_write_bytes(
            registry_path, json.dumps(registry, indent=2, sort_keys=True).encode("utf-8")
        )
        return target
