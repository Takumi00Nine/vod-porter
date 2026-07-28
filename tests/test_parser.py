"""S1 AC: M1 (subcommands + --help + unknown->2), M2 (defaults/priority),
M4 (exit 2 path)."""

from __future__ import annotations

import argparse

import pytest

from vodp.cli.parser import build_parser, parse_args

ALL_COMMANDS = [
    ("vods", "list"),
    ("vods", "upload"),
    ("videos", "list"),
    ("videos", "publish"),
    ("videos", "delete"),
    ("meta", "set"),
    ("playlist", "list"),
    ("playlist", "create"),
    ("playlist", "add"),
    ("playlist", "delete"),
    ("playlist", "remove-item"),
    ("cache", "clean"),
    ("ledger", "recover"),
    ("ledger", "repair"),
    ("ledger", "resolve"),
    ("auth", "login"),
    ("auth", "migrate"),
    ("launchd", "generate"),
]


def test_build_parser_returns_argparse_parser():
    parser = build_parser()
    assert isinstance(parser, argparse.ArgumentParser)


@pytest.mark.parametrize("command,subcommand", ALL_COMMANDS)
def test_help_exits_zero(command, subcommand, capsys):
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args([command, subcommand, "--help"])
    assert exc_info.value.code == 0


@pytest.mark.parametrize("command", ["check", "whoami"])
def test_top_level_help_exits_zero(command):
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args([command, "--help"])
    assert exc_info.value.code == 0


def test_unknown_command_exits_2():
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["not-a-real-command"])
    assert exc_info.value.code == 2


def test_unknown_subcommand_exits_2():
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["vods", "not-a-real-subcommand"])
    assert exc_info.value.code == 2


def test_unknown_choice_value_exits_2():
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args([
            "vods", "upload", "--privacy", "not-a-real-choice",
        ])
    assert exc_info.value.code == 2


def test_vods_list_requires_range():
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["vods", "list"])
    assert exc_info.value.code == 2


def test_vods_upload_defaults():
    args = parse_args(["vods", "upload"])
    assert args.max_uploads == 5  # J⑤
    assert args.allow_empty is False
    assert args.dry_run is False
    assert args.privacy is None  # resolved later by Settings (CLI > profile > default)
    assert args.profile == "default"
    assert args.deadline == 3600.0  # M15
    assert args.playlist_id is None


def test_vods_upload_deadline_override():
    args = parse_args(["vods", "upload", "--deadline", "120"])
    assert args.deadline == 120.0


def test_vods_upload_playlist_id_override():
    args = parse_args(["vods", "upload", "--playlist-id", "PLabc"])
    assert args.playlist_id == "PLabc"


def test_cli_overrides_default_via_namespace():
    args = parse_args(["vods", "upload", "--max-uploads", "9"])
    assert args.max_uploads == 9


def test_delete_requires_yes_flag_absent_by_default():
    args = parse_args(["videos", "delete", "--video-id", "abc"])
    assert args.yes is False


def test_parse_args_sets_subcommand_none_for_singleton_commands():
    args = parse_args(["whoami"])
    assert args.command == "whoami"
    assert args.subcommand is None


def test_vods_upload_playlist_and_playlist_id_mutually_exclusive():
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["vods", "upload", "--playlist", "Archive", "--playlist-id", "PL1"])
    assert exc_info.value.code == 2


def test_vods_upload_playlist_privacy_default_none_resolved_by_settings():
    args = parse_args(["vods", "upload"])
    assert args.playlist_privacy is None


def test_meta_set_has_playlist_id_but_not_playlist_name():
    args = parse_args(["meta", "set", "--video-id", "v1", "--playlist-id", "PL1"])
    assert args.playlist_id == "PL1"
    assert not hasattr(args, "playlist")  # meta set never resolves/creates a playlist by name
    assert not hasattr(args, "playlist_privacy")
