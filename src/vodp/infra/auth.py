"""YouTube OAuth state machine (M6/S3, design.md §5 -- 10 states A-J).

``authenticate()`` is the single entry point every command uses to get a
valid ``Credentials`` object; it never leaves the caller guessing which
exit code a given failure maps to -- each state below raises either
``AuthRequiredError`` (4, "run `vodp auth login`") or ``EnvironmentError_``
(5, "fix your setup, no amount of interactive login helps").

State -> outcome map (design.md §5):
  A no token file                    -> AuthRequiredError (non-interactive) / login flow (interactive)
  B corrupt token file (.bak backup)  -> same as A
  C expired, refresh_token present    -> refresh() and continue
  D refresh() raises RefreshError     -> AuthRequiredError, never falls back to login automatically
  E invalid/absent creds + no client_secret.json -> EnvironmentError_ (valid creds need no client_secret at all)
  F expired, no refresh_token         -> same as A
  G structurally malformed (token/client_id missing) -> same as A (Credentials.valid is naturally False)
  H stored scopes don't cover required scopes -> AuthRequiredError, always, both modes
  I 401/invalid_grant during *use* (not here -- see infra/youtube.py)
  J token file unreadable (PermissionError) -> EnvironmentError_
"""

from __future__ import annotations

import contextlib
import json
import pickle
import sys
from datetime import datetime
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from vodp.core.errors import AuthRequiredError, EnvironmentError_
from vodp.infra import fsops
from vodp.infra.locking import exclusive_lock

SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",
)


def token_path(home) -> Path:
    return Path(home) / "auth" / "token.json"


def client_secret_path(home) -> Path:
    return Path(home) / "auth" / "client_secret.json"


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


def _read_token_file(path: Path):
    """Returns None (absent), "corrupt" (unparseable), or the parsed dict."""
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except PermissionError:
        raise
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return "corrupt"


def _quarantine(path: Path) -> None:
    if path.exists():
        path.replace(path.with_name(path.name + ".bak"))


def _parse_expiry(value):
    if not value:
        return None
    return datetime.fromisoformat(value)


def _creds_from_dict(data: dict) -> Credentials:
    return Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
        scopes=list(data.get("scopes") or []),
        expiry=_parse_expiry(data.get("expiry")),
    )


def _creds_to_dict(creds: Credentials) -> dict:
    return {
        "type": "authorized_user",
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or []),
        "expiry": creds.expiry.isoformat() if creds.expiry else None,
    }


def save_credentials(path: Path, creds: Credentials) -> None:
    with exclusive_lock(_lock_path(path)):
        data = json.dumps(_creds_to_dict(creds), indent=2).encode("utf-8")
        fsops.atomic_write_bytes(path, data, mode=0o600)


def _run_login_flow(csp: Path, scopes) -> Credentials:
    flow = InstalledAppFlow.from_client_secrets_file(str(csp), list(scopes))
    with contextlib.redirect_stdout(sys.stderr):
        return flow.run_local_server(port=0)


def authenticate(
    home,
    *,
    non_interactive: bool,
    required_scopes=SCOPES,
    request_factory=Request,
    login_flow=_run_login_flow,
    dry_run: bool = False,
) -> Credentials:
    """``dry_run=True`` (M7 boundary): reads are unrestricted and an
    in-memory token refresh is still allowed (the design explicitly calls
    this out as the one write-shaped operation dry-run may still do), but
    nothing ever touches the filesystem -- no ``.bak`` quarantine of a
    corrupt token, no persisting a refreshed token, no login flow (that's
    already covered by forcing ``non_interactive`` for dry-run callers,
    kept here too as a second, independent guard)."""
    tp = token_path(home)
    csp = client_secret_path(home)

    try:
        raw = _read_token_file(tp)
    except PermissionError as e:
        raise EnvironmentError_(f"cannot read {tp}: {e}") from e  # state J

    creds = None
    if raw is None:
        pass  # state A
    elif raw == "corrupt":
        if not dry_run:
            _quarantine(tp)  # state B
    else:
        creds = _creds_from_dict(raw)  # states C/D/F/G/H/I depending on contents

    if creds is not None:
        if required_scopes and not set(required_scopes) <= set(creds.scopes or []):
            raise AuthRequiredError(  # state H
                "stored token scopes do not cover the required scopes; run `vodp auth login`"
            )
        if creds.valid:
            return creds
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(request_factory())  # state C (memory-only; M7 allows this in dry-run)
            except RefreshError as e:
                raise AuthRequiredError(  # state D
                    f"token refresh failed ({e}); run `vodp auth login`"
                ) from e
            if not dry_run:
                save_credentials(tp, creds)
            return creds
        # else: expired w/o refresh_token (F), or structurally invalid (G) -> fresh login below

    if not csp.exists():
        raise EnvironmentError_(  # state E
            f"{csp} not found; download it from Google Cloud Console"
        )
    if non_interactive or dry_run:
        raise AuthRequiredError(
            "no usable YouTube credentials and --non-interactive/--dry-run forbids "
            "opening a browser; run `vodp auth login` first"
        )
    creds = login_flow(csp, required_scopes)
    save_credentials(tp, creds)
    return creds


def migrate_pickle(home, pickle_path) -> dict:
    """S3: legacy token.pickle -> token.json (0600). Refuses to clobber an
    existing token.json (idempotent -- running it twice is a no-op error,
    not data loss)."""
    tp = token_path(home)
    pickle_path = Path(pickle_path)
    if tp.exists():
        raise EnvironmentError_(f"{tp} already exists; auth migrate will not overwrite it")
    if not pickle_path.exists():
        raise EnvironmentError_(f"pickle file not found: {pickle_path}")
    with open(pickle_path, "rb") as f:
        creds = pickle.load(f)
    save_credentials(tp, creds)
    migrated_marker = pickle_path.with_name(pickle_path.name + ".migrated")
    pickle_path.replace(migrated_marker)
    return {"token_path": str(tp), "migrated_pickle": str(migrated_marker)}
