"""M21 AC (R6, Codex/tester review round 2): a real, *non-editable*
``pip install`` of vodp, verified to actually work -- not just the
editable install (``pip install -e .``) every other test/dev workflow
uses, which happens to paper over a packaging bug because its import
path always resolves straight back into the repo checkout regardless of
where package data physically lives.

This is exactly how the bug this test pins was found: ``profiles/`` used
to live at the *repo root*, outside ``[tool.setuptools.packages.find]``'s
``where = ["src"]`` entirely -- invisible to any non-editable install's
package data -- so a real ``pip install .`` would produce a ``vodp`` that
can never find its own default profile (``load_profile`` raising
``EnvironmentError_`` for literally every ``--profile`` value, including
the packaged default). Fixed by moving the packaged defaults to
``src/vodp/profiles/`` and declaring them in
``[tool.setuptools.package-data]``; this test is what would have caught
the regression before an operator ever hit it for real.

Kept fully offline (no PyPI fetch) by using ``--no-build-isolation``
against *this* interpreter's own setuptools/wheel (dev dependency, see
pyproject.toml) and installing into a throwaway ``--prefix`` instead of
spinning up a whole nested venv -- ``--no-deps`` is safe here because
none of the assertions below need vodp's heavy runtime dependencies
(google-api-python-client, yt_dlp, ...) importable, only argparse's
``--help`` path and ``vodp.core.profile`` (see main.py's own comment on
why ``--help`` never needs them either).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _site_packages_dir(prefix: Path) -> Path:
    return prefix / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"


@pytest.fixture(scope="module")
def installed_prefix(tmp_path_factory):
    try:
        import setuptools  # noqa: F401
        import wheel  # noqa: F401
    except ImportError:
        pytest.skip("setuptools/wheel not importable in this interpreter -- `pip install -e '.[dev]'` first")

    prefix = tmp_path_factory.mktemp("vodp_pip_install_prefix")
    result = subprocess.run(
        [
            sys.executable, "-m", "pip", "install",
            "--no-build-isolation", "--no-deps", "--quiet",
            # --ignore-installed: without it, pip's resolver treats *this*
            # interpreter's own site-packages as ground truth even though
            # we're targeting a throwaway --prefix, and "helpfully"
            # uninstalls this repo's own editable `vodp` from the dev venv
            # as a side effect of "reinstalling" it elsewhere -- breaking
            # every other test in the same pytest run/venv (found the hard
            # way while writing this test).
            "--ignore-installed",
            "--prefix", str(prefix), str(REPO_ROOT),
        ],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"pip install failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    return prefix


def test_non_editable_install_console_script_works_from_arbitrary_cwd(installed_prefix, tmp_path):
    vodp_bin = installed_prefix / "bin" / "vodp"
    assert vodp_bin.is_file(), "console_scripts entry point (vodp=vodp.cli.main:main) was not installed"

    arbitrary_cwd = tmp_path / "somewhere_else_entirely"
    arbitrary_cwd.mkdir()
    # --prefix (unlike a real venv) doesn't put its own site-packages on
    # the interpreter's default sys.path -- PYTHONPATH stands in for what
    # a real venv's site.py would wire up automatically.
    result = subprocess.run(
        [str(vodp_bin), "--help"], cwd=str(arbitrary_cwd), capture_output=True, text=True, timeout=30,
        env={"PYTHONPATH": str(_site_packages_dir(installed_prefix))},
    )
    assert result.returncode == 0, result.stderr
    assert "vods" in result.stdout


def test_non_editable_install_ships_and_finds_its_own_default_profile(installed_prefix, tmp_path):
    """The exact regression this AC exists for: a non-editable install's
    packaged ``profiles/default.toml`` must be importable/loadable from
    package data, not from a repo-root path that doesn't exist once the
    package has been physically copied into site-packages."""
    site_packages = _site_packages_dir(installed_prefix)
    assert (site_packages / "vodp" / "profiles" / "default.toml").is_file(), (
        "profiles/*.toml was not installed as vodp package data -- "
        "check [tool.setuptools.package-data] in pyproject.toml"
    )

    arbitrary_cwd = tmp_path / "another_arbitrary_cwd"
    arbitrary_cwd.mkdir()
    nonexistent_home = tmp_path / "home_that_has_no_profiles_override"
    code = (
        "from vodp.core.profile import load_profile, PACKAGE_PROFILES_DIR\n"
        "import pathlib, sys\n"
        f"assert pathlib.Path({str(site_packages / 'vodp' / 'profiles')!r}) == PACKAGE_PROFILES_DIR, PACKAGE_PROFILES_DIR\n"
        f"p = load_profile(pathlib.Path({str(nonexistent_home)!r}), 'default')\n"
        "print('OK', p.name)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(arbitrary_cwd), capture_output=True, text=True, timeout=30,
        env={"PYTHONPATH": str(site_packages)},
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "OK default" in result.stdout
