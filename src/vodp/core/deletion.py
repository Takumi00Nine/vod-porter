"""Shared guard for every "default dry-run, --yes to execute" command
(videos delete, playlist delete/remove-item, cache clean -- M1改).

Interactively, omitting --yes is simply a safe preview (exit 0). Under
--non-interactive, omitting --yes is instead an argument error (exit 2):
unattended jobs must say what they mean instead of silently no-op'ing
forever.
"""

from __future__ import annotations

from vodp.core.errors import ArgError


def resolve_execute(args) -> bool:
    non_interactive = bool(getattr(args, "non_interactive", False))
    yes = bool(getattr(args, "yes", False))
    if non_interactive and not yes:
        raise ArgError("--yes is required together with --non-interactive for destructive commands")
    if getattr(args, "dry_run", False):
        return False
    return yes
