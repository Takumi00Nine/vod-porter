"""Secrets: <VODP_HOME>/.env is the file source, real env vars always win."""

from __future__ import annotations

import pytest

from vodp.core.errors import EnvironmentError_
from vodp.infra.envconfig import load_env, require_twitch_config


def test_loads_values_from_env_file(vodp_home):
    (vodp_home / ".env").write_text(
        "TWITCH_CLIENT_ID=file_id\nTWITCH_CLIENT_SECRET=file_secret\nTWITCH_CHANNEL_LOGIN=filechan\n",
        encoding="utf-8",
    )
    values = load_env(vodp_home, environ={})
    assert values["TWITCH_CLIENT_ID"] == "file_id"


def test_real_env_var_overrides_file(vodp_home):
    (vodp_home / ".env").write_text("TWITCH_CLIENT_ID=file_id\n", encoding="utf-8")
    values = load_env(vodp_home, environ={"TWITCH_CLIENT_ID": "env_id"})
    assert values["TWITCH_CLIENT_ID"] == "env_id"


def test_missing_file_returns_env_only(vodp_home):
    values = load_env(vodp_home, environ={"TWITCH_CLIENT_ID": "env_id"})
    assert values["TWITCH_CLIENT_ID"] == "env_id"


def test_require_twitch_config_raises_environment_error_when_missing(vodp_home):
    with pytest.raises(EnvironmentError_):
        require_twitch_config(load_env(vodp_home, environ={}))


def test_require_twitch_config_ok_when_present(vodp_home):
    env = {
        "TWITCH_CLIENT_ID": "a", "TWITCH_CLIENT_SECRET": "b", "TWITCH_CHANNEL_LOGIN": "c",
    }
    result = require_twitch_config(load_env(vodp_home, environ=env))
    assert result == env
