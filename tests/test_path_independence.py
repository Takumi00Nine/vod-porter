"""M21 AC-3b: vodp must work from any cwd, with no hardcoded paths baked
into src/vodp (see also test_no_hardcoded_strings.py's grep AC)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_python_m_vodp_help_works_from_arbitrary_cwd(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO_ROOT / "src")}
    result = subprocess.run(
        [sys.executable, "-m", "vodp", "--help"],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "vods" in result.stdout


def test_python_m_vodp_whoami_help_from_arbitrary_cwd(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO_ROOT / "src")}
    result = subprocess.run(
        [sys.executable, "-m", "vodp", "whoami", "--help"],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
