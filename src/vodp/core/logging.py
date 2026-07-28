"""M19 structured logging: logs/YYYY-MM-DD.log, 1 line = 1 JSON, JST, run_id.

Secrets are masked at the formatter layer (never rely on callers to avoid
logging them) and a single run is hard-capped to avoid runaway disk usage
from e.g. a stuck retry loop.

Cap enforcement is per-process, best-effort, not cross-process-locked
(F7, Codex/tester review round 2/3, differed-items follow-up): each
``open(path, "a")`` append is a plain, unlocked write, so two runs racing
right at the cap boundary could each still write past it in the same
narrow window. What *is* guarded against is the more likely case of many
runs, one after another over the same day, each seeing an
already-capped file and every one of them re-appending its own
"log_cap_reached" marker forever -- ``_tail_is_already_marked`` checks
the file's actual last line before writing another one.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

JST = timezone(timedelta(hours=9))
HARD_CAP_BYTES = 5 * 1024 * 1024  # 5 MiB
DEFAULT_RETENTION_DAYS = 30


class JsonLineLogger:
    def __init__(self, home: Path, run_id: str, secrets: list[str] | None = None):
        self.logs_dir = Path(home) / "logs"
        self.run_id = run_id
        self.secrets = [s for s in (secrets or []) if s]
        self._capped = False
        self._lock = threading.Lock()

    def add_secrets(self, values: list[str | None]) -> None:
        """Register additional secret values to mask -- for callers (e.g.
        ``vods upload``) that only learn some secrets (OAuth token/
        refresh_token/client_secret) *after* the logger itself already
        had to be constructed (R12, Codex/tester review round 2: the real
        pipeline never actually passed ``secrets=`` at all, so masking
        only ever worked in this class's own isolated unit tests, never
        against a real run's log output)."""
        with self._lock:
            self.secrets.extend(v for v in values if v)

    def _mask(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "***")
        return text

    def _path_for_now(self) -> Path:
        today = datetime.now(JST).strftime("%Y-%m-%d")
        return self.logs_dir / f"{today}.log"

    def log(self, level: str, event: str, **fields: Any) -> None:
        with self._lock:
            if self._capped:
                return
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            path = self._path_for_now()
            now = datetime.now(JST).isoformat()
            record = {"ts": now, "run_id": self.run_id, "level": level, "event": event}
            record.update(fields)
            line = self._mask(json.dumps(record, ensure_ascii=False, default=str))
            size = path.stat().st_size if path.exists() else 0
            # F7 (Codex/tester review round 2, differed-items follow-up,
            # judged cheap enough to just fix): check against what the
            # file size *would become* after writing this line, not the
            # size before it -- comparing against the pre-write size let
            # one (potentially large) line push the file past
            # HARD_CAP_BYTES by that line's own length instead of
            # stopping right at the cap. The one-time "log_cap_reached"
            # marker itself is still always written unconditionally once
            # capped, so the true final ceiling is
            # ``HARD_CAP_BYTES + len(marker line)`` -- a small, fixed,
            # one-time overshoot bounded by the marker's own (short,
            # constant-shape) size, not an arbitrary caller-supplied line.
            projected_size = size + len(line.encode("utf-8")) + 1  # +1 for the trailing newline
            if projected_size > HARD_CAP_BYTES:
                self._capped = True
                # Codex light review (round-3 follow-up on F7): ``_capped``
                # is per-*instance* -- a separate process/run started
                # later the same day (a fresh JsonLineLogger, same log
                # file) has no way to know a previous one already wrote
                # the marker, and would otherwise re-append its own
                # "log_cap_reached" line on its very first log() call,
                # every single time, for as many runs as still start that
                # day -- an unbounded pile-up across runs even though each
                # individual run is itself correctly capped. Check the
                # file's own last line first and skip if it's already the
                # marker.
                if self._tail_is_already_marked(path):
                    return
                marker = json.dumps(
                    {"ts": now, "run_id": self.run_id, "level": "warning", "event": "log_cap_reached"}
                )
                with open(path, "a", encoding="utf-8") as f:
                    f.write(marker + "\n")
                return
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    @staticmethod
    def _tail_is_already_marked(path: Path) -> bool:
        """Best-effort, not a substitute for real cross-process locking
        (this class never flock()s its append writes at all) -- but
        cheap enough to close the "repeated marker every run" gap without
        the bigger surgery a real lock would need."""
        try:
            with open(path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 4096))
                tail = f.read().decode("utf-8", errors="ignore")
        except OSError:
            return False
        stripped = tail.strip()
        if not stripped:
            return False
        last_line = stripped.splitlines()[-1]
        try:
            return json.loads(last_line).get("event") == "log_cap_reached"
        except (json.JSONDecodeError, ValueError):
            return False

    def purge_old(self, retention_days: int = DEFAULT_RETENTION_DAYS) -> None:
        if not self.logs_dir.exists():
            return
        cutoff = datetime.now(JST).date() - timedelta(days=retention_days)
        for p in self.logs_dir.glob("*.log"):
            try:
                day = datetime.strptime(p.stem, "%Y-%m-%d").date()
            except ValueError:
                continue
            if day < cutoff:
                p.unlink(missing_ok=True)


class NullLogger:
    """Used for --dry-run: logs/ must not be created (M6/M7 boundary)."""

    def log(self, level: str, event: str, **fields: Any) -> None:
        return

    def add_secrets(self, values: list[str | None]) -> None:
        return

    def purge_old(self, retention_days: int = DEFAULT_RETENTION_DAYS) -> None:
        return
