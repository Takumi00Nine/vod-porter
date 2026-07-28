"""M9 AC: profile not found -> exit 5 (EnvironmentError_); bad template key
inside a profile -> exit 2 (ArgError, surfaced at load time)."""

from __future__ import annotations

import pytest

from vodp.core.errors import ArgError, EnvironmentError_
from vodp.core.profile import load_profile


def test_missing_profile_raises_environment_error(vodp_home):
    with pytest.raises(EnvironmentError_):
        load_profile(vodp_home, "does-not-exist")


def test_profile_in_home_overrides_package_default(vodp_home):
    (vodp_home / "profiles").mkdir()
    (vodp_home / "profiles" / "custom.toml").write_text(
        """
        [meta]
        title_template = "{title} custom"
        category_id = "20"
        tags = ["a", "b"]

        [playlist]
        name = "MyList"
        privacy = "unlisted"

        [limits]
        earliest_date = "2026-01-01"
        """,
        encoding="utf-8",
    )
    profile = load_profile(vodp_home, "custom")
    assert profile.title_template == "{title} custom"
    assert profile.category_id == "20"
    assert profile.tags == ["a", "b"]
    assert profile.playlist_name == "MyList"
    assert profile.playlist_privacy == "unlisted"
    assert profile.earliest_date == "2026-01-01"


def test_profile_with_unknown_template_var_raises_arg_error(vodp_home):
    (vodp_home / "profiles").mkdir()
    (vodp_home / "profiles" / "bad.toml").write_text(
        """
        [meta]
        title_template = "{not_a_real_var}"
        """,
        encoding="utf-8",
    )
    with pytest.raises(ArgError):
        load_profile(vodp_home, "bad")


def test_package_example_profile_loads_and_validates(vodp_home):
    profile = load_profile(vodp_home, "example")
    assert profile.title_template
    assert profile.description_template
