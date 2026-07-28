"""M9/M21 AC: src/vodp must contain no absolute /Users/ paths and no
channel-specific literal strings (those live only in profiles/*.toml)."""

from __future__ import annotations

import pathlib
import subprocess

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src" / "vodp"


def test_no_absolute_user_paths_in_src():
    result = subprocess.run(
        ["grep", "-R", "-n", "--include=*.py", "--include=*.json", "--include=*.toml", "/Users/", str(SRC_DIR)],
        capture_output=True, text=True,
    )
    assert result.returncode == 1, f"found /Users/ references:\n{result.stdout}"


def test_no_home_env_var_literal_paths_in_src():
    # os.chdir is banned outright (G21/M21改)
    result = subprocess.run(
        ["grep", "-R", "-n", "--include=*.py", "os.chdir", str(SRC_DIR)],
        capture_output=True, text=True,
    )
    assert result.returncode == 1, f"found os.chdir usage:\n{result.stdout}"


def test_no_hardcoded_category_id_default_in_engine_code():
    """R7 (Codex/tester review round 2): a YouTube ``categoryId`` is a
    channel-specific value, exactly like playlist name/tags/earliest_date
    -- it must come from ``--category-id`` or a profile's own ``[meta]``
    table, never a literal baked into ``src/vodp``'s own *code* as a
    fallback default (``settings.py``'s ``BUILTIN_DEFAULTS`` used to carry
    ``"category_id": "22"``, and ``core/profile.py``'s ``Profile``
    dataclass carried the same "22" as its field default -- both silently
    supplied a value even when neither the CLI nor the profile file ever
    set one).

    ``.toml`` is deliberately excluded from this scan: profiles/*.toml
    (packaged defaults *and* a real per-install ``<VODP_HOME>/profiles/``
    override) are exactly where a literal ``category_id = "22"`` is
    supposed to live."""
    result = subprocess.run(
        [
            "grep", "-R", "-n", "-E", "--include=*.py",
            r'category_id.{0,20}["\x27][0-9]+["\x27]',
            str(SRC_DIR),
        ],
        capture_output=True, text=True,
    )
    assert result.returncode == 1, f"found a hardcoded categoryId literal in src/vodp/*.py:\n{result.stdout}"
