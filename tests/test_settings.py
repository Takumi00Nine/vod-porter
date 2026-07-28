"""M9/R7 AC (Codex/tester review round 2): categoryId is channel-specific
config, resolved CLI > profile, with no engine-side numeric fallback."""

from __future__ import annotations

import types

import pytest

from vodp.cli.settings import resolve_settings
from vodp.core.errors import ArgError, EnvironmentError_
from vodp.core.profile import Profile


def _args(**overrides):
    base = {
        "title": None, "description_file": None, "privacy": None,
        "playlist": None, "playlist_id": None, "playlist_privacy": None,
        "tags": None, "category_id": None, "max_uploads": None,
        "deadline": None, "allow_empty": False, "dry_run": False,
        "non_interactive": False,
    }
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _profile(**overrides):
    return Profile(name="default", **overrides)


def test_missing_category_id_in_both_cli_and_profile_is_environment_error():
    """Neither --category-id nor the profile's [meta] category_id was
    ever set -- must fail closed (5), never silently fall back to a
    src/vodp-baked value."""
    with pytest.raises(EnvironmentError_):
        resolve_settings(_args(), _profile(category_id=None))


def test_profile_category_id_used_when_cli_omits_it():
    settings = resolve_settings(_args(), _profile(category_id="20"))
    assert settings.category_id == "20"


def test_cli_category_id_overrides_profile():
    settings = resolve_settings(_args(category_id="10"), _profile(category_id="20"))
    assert settings.category_id == "10"


def test_empty_cli_category_id_is_arg_error_not_silently_accepted():
    """Codex light review (round-2 follow-up on R7): _first_not_none()
    treats "" as a valid value, so an empty --category-id used to sail
    straight past the None-check and reach insert_video's body."""
    with pytest.raises(ArgError):
        resolve_settings(_args(category_id=""), _profile(category_id="20"))


def test_whitespace_only_cli_category_id_is_arg_error():
    with pytest.raises(ArgError):
        resolve_settings(_args(category_id="   "), _profile(category_id="20"))


def test_empty_profile_category_id_is_environment_error_not_silently_accepted():
    with pytest.raises(EnvironmentError_):
        resolve_settings(_args(), _profile(category_id=""))
