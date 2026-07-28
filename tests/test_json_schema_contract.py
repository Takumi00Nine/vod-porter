"""M8 AC: every command's stdout JSON validates against its
schemas/<command>.json (jsonschema, dev dependency per design.md §10/★4)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from vodp.cli.main import main
from vodp.core import exitcodes

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "src" / "vodp" / "schemas"


def _validate(command_schema_name, envelope):
    schema = json.loads((SCHEMAS_DIR / f"{command_schema_name}.json").read_text())
    jsonschema.validate(instance=envelope, schema=schema)


def _run(argv, capsys):
    """M4/M8: exit 2 never carries a JSON envelope (argparse failure or a
    post-parse ArgError, both no-JSON per design.md §3) -- every other
    exit code always does. Returns (code, parsed_json_or_None)."""
    code = main(argv)
    raw = capsys.readouterr().out
    if code == exitcodes.ARG_ERROR:
        assert raw == "", f"exit 2 must not emit stdout JSON, got: {raw!r}"
        return code, None
    return code, json.loads(raw)


def test_check_output_matches_schema(vodp_home, capsys):
    # M4 (R3): a bare vodp_home has no .env, which is an EnvironmentError_
    # (5) per check.py's own natural exit code, preserved (not downgraded
    # to 6) by exitcodes.combine().
    code, out = _run(["check", "--home", str(vodp_home)], capsys)
    assert code in (exitcodes.OK, exitcodes.ENV_OR_API_ERROR)
    _validate("check", out)


def test_whoami_missing_auth_output_matches_schema(vodp_home, capsys):
    from vodp.infra.auth import client_secret_path

    client_secret_path(vodp_home).parent.mkdir(parents=True, exist_ok=True)
    client_secret_path(vodp_home).write_text("{}", encoding="utf-8")
    code, out = _run(["whoami", "--home", str(vodp_home), "--non-interactive"], capsys)
    assert code == exitcodes.AUTH_REQUIRED
    _validate("whoami", out)


def test_ledger_recover_output_matches_schema(vodp_home, capsys):
    code, out = _run(["ledger", "recover", "--home", str(vodp_home), "--channel", "UCabc"], capsys)
    assert code == exitcodes.OK
    _validate("ledger_recover", out)


def test_vods_list_missing_env_matches_schema(vodp_home, capsys):
    code, out = _run(["vods", "list", "--home", str(vodp_home), "--range", "2026-07-01", "2026-07-01"], capsys)
    assert code == exitcodes.ENV_OR_API_ERROR
    _validate("vods_list", out)


def test_cache_clean_dry_run_matches_schema(vodp_home, capsys):
    code, out = _run(["cache", "clean", "--home", str(vodp_home)], capsys)
    assert code == exitcodes.OK
    _validate("cache_clean", out)


def test_auth_migrate_missing_pickle_is_arg_error_with_no_json(vodp_home, capsys):
    # M4/M8: exit 2 never carries a JSON envelope -- _run() itself asserts
    # stdout is empty for this code, so there is nothing left to validate
    # against a schema here.
    code, out = _run(
        ["auth", "migrate", "--home", str(vodp_home), "--pickle-path", str(vodp_home / "nope.pickle")], capsys
    )
    assert code == exitcodes.ARG_ERROR
    assert out is None


def test_auth_migrate_success_matches_schema(vodp_home, tmp_path, capsys):
    import pickle

    from google.oauth2.credentials import Credentials

    from vodp.infra.auth import SCOPES

    creds = Credentials(
        token="tok", refresh_token="refresh", token_uri="https://oauth2.googleapis.com/token",
        client_id="cid", client_secret="csecret", scopes=list(SCOPES),
    )
    pickle_path = tmp_path / "token.pickle"
    with open(pickle_path, "wb") as f:
        pickle.dump(creds, f)

    code, out = _run(["auth", "migrate", "--home", str(vodp_home), "--pickle-path", str(pickle_path)], capsys)
    assert code == exitcodes.OK
    _validate("auth_migrate", out)


def test_launchd_generate_matches_schema(vodp_home, tmp_path, capsys):
    code, out = _run([
        "launchd", "generate", "--home", str(vodp_home),
        "--venv-python", "/abs/.venv/bin/vodp", "--working-directory", "/abs",
        "--out", str(tmp_path / "x.plist"),
    ], capsys)
    assert code == exitcodes.OK
    _validate("launchd_generate", out)


@pytest.mark.parametrize("schema_file", sorted(p.name for p in SCHEMAS_DIR.glob("*.json")))
def test_every_schema_file_is_valid_json_schema(schema_file):
    schema = json.loads((SCHEMAS_DIR / schema_file).read_text())
    jsonschema.Draft7Validator.check_schema(schema)
