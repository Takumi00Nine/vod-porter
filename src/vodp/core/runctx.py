"""RunContext: the per-invocation bag of resolved settings/paths/services.

Built once in vodp.cli.main.main() and threaded through every command so
that no module reaches for globals, env vars, or hardcoded paths directly
(M21: src/vodp must stay path- and identity-agnostic).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

JST = timezone(timedelta(hours=9))


def resolve_home(cli_home: str | None, env: dict[str, str]) -> Path:
    """--home > VODP_HOME > package default (~/.vodp)."""
    if cli_home:
        return Path(cli_home).expanduser().resolve()
    env_home = env.get("VODP_HOME")
    if env_home:
        return Path(env_home).expanduser().resolve()
    return Path.home() / ".vodp"


@dataclass
class RunContext:
    home: Path
    command: str
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: datetime = field(default_factory=lambda: datetime.now(JST))
    dry_run: bool = False
    non_interactive: bool = False
    settings: Any = None
    logger: Any = None
    secrets: list[str] = field(default_factory=list)

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def ledger_dir(self) -> Path:
        return self.home / "ledger"

    @property
    def run_dir(self) -> Path:
        return self.home / "run"

    @property
    def auth_dir(self) -> Path:
        return self.home / "auth"

    @property
    def downloads_dir(self) -> Path:
        return self.home / "downloads"

    def run_header(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "command": self.command,
            "started_at": self.started_at.isoformat(),
            "dry_run": self.dry_run,
        }

    def register_secrets(self, values) -> None:
        """M19 AC (requirements.md M19: "sentinel secret が stdout/
        stderr/ログに不出現") -- a single place every command registers a
        secret it learns, so both ``main.py``'s stderr/JSON output *and*
        ``self.logger``'s log-file output mask the same values. Fixes a
        gap where R12 only ever wired secrets into the log file, never
        into what main.py prints for an exception raised after the
        secret was already loaded (Codex light review, round-2 follow-up)."""
        for v in values:
            if v and v not in self.secrets:
                self.secrets.append(v)
        if self.logger is not None:
            add_secrets = getattr(self.logger, "add_secrets", None)
            if add_secrets is not None:
                add_secrets(values)

    def redact(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "***")
        return text
