"""M6 AC: all 10 auth states (design.md §5) resolve to the documented
exception/exit-code, and non_interactive never invokes the login flow."""

from __future__ import annotations

import json
import pickle
from datetime import datetime, timedelta

import pytest
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials

from vodp.core.errors import AuthRequiredError, EnvironmentError_
from vodp.infra.auth import SCOPES, authenticate, client_secret_path, migrate_pickle, token_path


def _write_client_secret(home):
    client_secret_path(home).parent.mkdir(parents=True, exist_ok=True)
    client_secret_path(home).write_text(json.dumps({"installed": {"client_id": "x"}}), encoding="utf-8")


def _write_token(home, data: dict):
    token_path(home).parent.mkdir(parents=True, exist_ok=True)
    token_path(home).write_text(json.dumps(data), encoding="utf-8")


def _valid_token_dict(**overrides):
    base = {
        "token": "tok-abc",
        "refresh_token": "refresh-abc",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "cid",
        "client_secret": "csecret",
        "scopes": list(SCOPES),
        "expiry": (datetime.utcnow() + timedelta(hours=1)).isoformat(),
    }
    base.update(overrides)
    return base


def _login_flow_should_not_be_called(*a, **kw):
    raise AssertionError("login flow must not be invoked")


# -- A: no token file -------------------------------------------------------

def test_state_a_no_token_non_interactive_raises_auth_required(vodp_home):
    _write_client_secret(vodp_home)
    with pytest.raises(AuthRequiredError):
        authenticate(vodp_home, non_interactive=True, login_flow=_login_flow_should_not_be_called)


def test_state_a_no_token_interactive_runs_login_flow(vodp_home):
    _write_client_secret(vodp_home)
    fresh = Credentials(
        token="new-tok", refresh_token="new-refresh", token_uri="https://oauth2.googleapis.com/token",
        client_id="cid", client_secret="csecret", scopes=list(SCOPES),
    )
    creds = authenticate(vodp_home, non_interactive=False, login_flow=lambda csp, scopes: fresh)
    assert creds.token == "new-tok"
    assert token_path(vodp_home).exists()


# -- B: corrupt token file ---------------------------------------------------

def test_state_b_corrupt_token_quarantined_and_raises(vodp_home):
    _write_client_secret(vodp_home)
    token_path(vodp_home).parent.mkdir(parents=True, exist_ok=True)
    token_path(vodp_home).write_text("{not valid json", encoding="utf-8")
    with pytest.raises(AuthRequiredError):
        authenticate(vodp_home, non_interactive=True, login_flow=_login_flow_should_not_be_called)
    assert token_path(vodp_home).with_name("token.json.bak").exists()
    assert not token_path(vodp_home).exists()


# -- C: expired, refreshable --------------------------------------------------

def test_state_c_expired_refreshable_refreshes_and_saves(vodp_home):
    expired = _valid_token_dict(expiry=(datetime.utcnow() - timedelta(hours=1)).isoformat())
    _write_token(vodp_home, expired)

    def fake_request_factory():
        return object()

    def fake_refresh(self, request):
        self.token = "refreshed-tok"
        self.expiry = datetime.utcnow() + timedelta(hours=1)

    import vodp.infra.auth as auth_mod
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Credentials, "refresh", fake_refresh)
        creds = auth_mod.authenticate(
            vodp_home, non_interactive=True, request_factory=fake_request_factory,
            login_flow=_login_flow_should_not_be_called,
        )
    assert creds.token == "refreshed-tok"
    saved = json.loads(token_path(vodp_home).read_text())
    assert saved["token"] == "refreshed-tok"


# -- D: refresh fails ---------------------------------------------------------

def test_state_d_refresh_error_raises_auth_required_not_login(vodp_home):
    expired = _valid_token_dict(expiry=(datetime.utcnow() - timedelta(hours=1)).isoformat())
    _write_token(vodp_home, expired)

    def failing_refresh(self, request):
        raise RefreshError("invalid_grant")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Credentials, "refresh", failing_refresh)
        with pytest.raises(AuthRequiredError):
            authenticate(
                vodp_home, non_interactive=True, request_factory=lambda: object(),
                login_flow=_login_flow_should_not_be_called,
            )


# -- E: no client_secret + invalid creds -------------------------------------

def test_state_e_missing_client_secret_with_invalid_token_is_environment_error(vodp_home):
    with pytest.raises(EnvironmentError_):
        authenticate(vodp_home, non_interactive=True, login_flow=_login_flow_should_not_be_called)


def test_state_e_missing_client_secret_but_valid_token_still_works(vodp_home):
    _write_token(vodp_home, _valid_token_dict())
    creds = authenticate(vodp_home, non_interactive=True, login_flow=_login_flow_should_not_be_called)
    assert creds.token == "tok-abc"


# -- F: expired, no refresh_token ---------------------------------------------

def test_state_f_expired_no_refresh_token_raises_auth_required(vodp_home):
    _write_client_secret(vodp_home)
    data = _valid_token_dict(refresh_token=None, expiry=(datetime.utcnow() - timedelta(hours=1)).isoformat())
    _write_token(vodp_home, data)
    with pytest.raises(AuthRequiredError):
        authenticate(vodp_home, non_interactive=True, login_flow=_login_flow_should_not_be_called)


# -- G: structurally malformed (token missing) --------------------------------

def test_state_g_missing_token_field_raises_auth_required(vodp_home):
    _write_client_secret(vodp_home)
    data = _valid_token_dict(token=None, expiry=None)
    _write_token(vodp_home, data)
    with pytest.raises(AuthRequiredError):
        authenticate(vodp_home, non_interactive=True, login_flow=_login_flow_should_not_be_called)


# -- H: scope insufficient -----------------------------------------------------

def test_state_h_insufficient_scopes_raises_auth_required_even_if_valid(vodp_home):
    data = _valid_token_dict(scopes=["https://www.googleapis.com/auth/youtube.readonly"])
    _write_token(vodp_home, data)
    with pytest.raises(AuthRequiredError):
        authenticate(vodp_home, non_interactive=True, login_flow=_login_flow_should_not_be_called)


# -- J: permission error -------------------------------------------------------

def test_state_j_permission_error_is_environment_error(vodp_home, monkeypatch):
    _write_token(vodp_home, _valid_token_dict())

    import pathlib
    original_read_text = pathlib.Path.read_text

    def flaky_read_text(self, *a, **kw):
        if self == token_path(vodp_home):
            raise PermissionError("denied")
        return original_read_text(self, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "read_text", flaky_read_text)
    with pytest.raises(EnvironmentError_):
        authenticate(vodp_home, non_interactive=True, login_flow=_login_flow_should_not_be_called)


# -- migrate -------------------------------------------------------------------

def test_migrate_pickle_to_json(vodp_home, tmp_path):
    creds = Credentials(
        token="pickled-tok", refresh_token="pickled-refresh",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="cid", client_secret="csecret", scopes=list(SCOPES),
    )
    pickle_path = tmp_path / "token.pickle"
    with open(pickle_path, "wb") as f:
        pickle.dump(creds, f)

    result = migrate_pickle(vodp_home, pickle_path)
    saved = json.loads(token_path(vodp_home).read_text())
    assert saved["token"] == "pickled-tok"
    assert not pickle_path.exists()
    assert (tmp_path / "token.pickle.migrated").exists()


def test_migrate_pickle_refuses_to_overwrite_existing_token(vodp_home, tmp_path):
    _write_token(vodp_home, _valid_token_dict())
    pickle_path = tmp_path / "token.pickle"
    with open(pickle_path, "wb") as f:
        pickle.dump(object(), f)
    with pytest.raises(EnvironmentError_):
        migrate_pickle(vodp_home, pickle_path)


# -- dry_run boundary (M7, Codex/tester review round 2, R2) -------------------

def test_dry_run_corrupt_token_never_quarantined(vodp_home):
    token_path(vodp_home).parent.mkdir(parents=True, exist_ok=True)
    token_path(vodp_home).write_text("{not valid json", encoding="utf-8")
    _write_client_secret(vodp_home)
    with pytest.raises(AuthRequiredError):
        authenticate(vodp_home, non_interactive=True, dry_run=True, login_flow=_login_flow_should_not_be_called)
    assert token_path(vodp_home).exists()  # untouched
    assert not token_path(vodp_home).with_name("token.json.bak").exists()


def test_dry_run_refresh_is_memory_only_never_persisted(vodp_home):
    expired = _valid_token_dict(expiry=(datetime.utcnow() - timedelta(hours=1)).isoformat())
    _write_token(vodp_home, expired)

    def fake_refresh(self, request):
        self.token = "refreshed-tok"
        self.expiry = datetime.utcnow() + timedelta(hours=1)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Credentials, "refresh", fake_refresh)
        creds = authenticate(
            vodp_home, non_interactive=True, dry_run=True, request_factory=lambda: object(),
            login_flow=_login_flow_should_not_be_called,
        )
    assert creds.token == "refreshed-tok"  # refreshed in memory...
    saved = json.loads(token_path(vodp_home).read_text())
    assert saved["token"] == "tok-abc"  # ...but the file on disk is untouched


def test_dry_run_no_token_no_client_secret_raises_auth_required_not_env_error(vodp_home):
    # dry-run's own guard (non_interactive OR dry_run) fires before the
    # missing-client_secret check would; either way, no FS write happens.
    with pytest.raises((AuthRequiredError, EnvironmentError_)):
        authenticate(vodp_home, non_interactive=True, dry_run=True, login_flow=_login_flow_should_not_be_called)
    assert not token_path(vodp_home).exists()
