"""Secrets live only in ``<VODP_HOME>/.env`` or real environment variables
(migration plan §11 item 3) -- never in profiles/*.toml and never hardcoded
in src/vodp. Real environment variables always win over the file, so a
launchd EnvironmentVariables block or a shell export can override the file
without editing it.
"""

from __future__ import annotations

import os
from pathlib import Path

from vodp.core.errors import EnvironmentError_

REQUIRED_TWITCH_KEYS = ("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET", "TWITCH_CHANNEL_LOGIN")


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_env(home: Path, environ: dict[str, str] | None = None) -> dict[str, str]:
    """<home>/.env, overlaid by real process environment variables."""
    environ = environ if environ is not None else dict(os.environ)
    values: dict[str, str] = {}
    env_path = Path(home) / ".env"
    if env_path.is_file():
        values.update(_parse_env_file(env_path))
    for key in set(values) | {k for k in environ if k.startswith("TWITCH_") or k.startswith("VODP_")}:
        if key in environ:
            values[key] = environ[key]
    return values


def require_twitch_config(env: dict[str, str]) -> dict[str, str]:
    missing = [k for k in REQUIRED_TWITCH_KEYS if not env.get(k)]
    if missing:
        raise EnvironmentError_(
            "missing required Twitch config in <VODP_HOME>/.env or environment: "
            + ", ".join(missing)
        )
    return {k: env[k] for k in REQUIRED_TWITCH_KEYS}
