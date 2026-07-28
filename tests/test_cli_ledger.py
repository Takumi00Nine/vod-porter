"""End-to-end (argv -> exit code -> stdout JSON) coverage for the ledger
admin commands, wired through vodp.cli.main.main()."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from vodp.cli.main import main
from vodp.core import exitcodes
from vodp.infra.ledger import Ledger, resolve_ledger_path

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "src" / "vodp" / "schemas"


def _run(argv, capsys):
    code = main(argv)
    out = capsys.readouterr().out
    return code, json.loads(out)


def _validate(command_schema_name, envelope):
    """R11 (Codex/tester review round 2): validate real command output
    against its schemas/<command>.json, not just the format-agnostic
    assertions above."""
    schema = json.loads((SCHEMAS_DIR / f"{command_schema_name}.json").read_text())
    jsonschema.validate(instance=envelope, schema=schema)


def test_recover_lists_inflight_rows(vodp_home, capsys):
    path = resolve_ledger_path(vodp_home, "UCabc")
    ledger = Ledger(path, channel_id="UCabc")
    ledger.reserve(twitch_vod_id="v1", twitch_user_id="u1", profile="default", intent={"a": 1})
    ledger.advance(twitch_vod_id="v1", stage="insert_inflight")

    code, envelope = _run(
        ["ledger", "recover", "--home", str(vodp_home), "--channel", "UCabc"], capsys
    )
    assert code == exitcodes.OK
    assert envelope["videos"][0]["twitch_vod_id"] == "v1"
    assert envelope["videos"][0]["reason_code"] == "insert_inflight"
    _validate("ledger_recover", envelope)


def test_resolve_reinsert_via_cli(vodp_home, capsys):
    path = resolve_ledger_path(vodp_home, "UCabc")
    ledger = Ledger(path, channel_id="UCabc")
    ledger.reserve(twitch_vod_id="v1", twitch_user_id="u1", profile="default", intent={"a": 1})
    ledger.advance(twitch_vod_id="v1", stage="insert_inflight")
    ledger.advance(twitch_vod_id="v1", stage="recovery_required")

    code, envelope = _run(
        ["ledger", "resolve", "--home", str(vodp_home), "--channel", "UCabc",
         "--vod", "v1", "--action", "reinsert", "--yes"],
        capsys,
    )
    assert code == exitcodes.OK
    reloaded = Ledger(path, channel_id="UCabc")
    assert reloaded.get_current("v1")["stage"] == "reserved"
    _validate("ledger_resolve", envelope)


def test_resolve_without_yes_exits_2():
    from vodp.cli.parser import build_parser
    import pytest

    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["ledger", "resolve", "--channel", "UCabc", "--vod", "v1", "--action", "abandon"])
    assert exc_info.value.code == 2


def test_repair_on_healthy_ledger_is_a_noop(vodp_home, capsys):
    path = resolve_ledger_path(vodp_home, "UCabc")
    ledger = Ledger(path, channel_id="UCabc")
    ledger.reserve(twitch_vod_id="v1", twitch_user_id="u1", profile="default", intent={"a": 1})

    code, envelope = _run(
        ["ledger", "repair", "--home", str(vodp_home), "--channel", "UCabc"], capsys
    )
    assert code == exitcodes.OK
    assert envelope["summary"]["repaired"] is False
    _validate("ledger_repair", envelope)


def test_repair_quarantines_and_exits_5_when_not_at_tail(vodp_home, capsys):
    ledger_dir = vodp_home / "ledger"
    ledger_dir.mkdir(parents=True)
    path = ledger_dir / "UCabc.jsonl"
    path.write_text('garbage\n{"kind": "state", "twitch_vod_id": "v1", "stage": "reserved"}\n', encoding="utf-8")
    (ledger_dir / "registry.json").write_text(json.dumps({"UCabc": str(path)}), encoding="utf-8")

    code, envelope = _run(
        ["ledger", "repair", "--home", str(vodp_home), "--channel", "UCabc"], capsys
    )
    assert code == exitcodes.ENV_OR_API_ERROR
    assert envelope["summary"]["blocked"] is True
    _validate("ledger_repair", envelope)
