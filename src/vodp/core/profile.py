"""M9 profile loading:固有値 (title/description template, tags, categoryId,
playlist name/privacy, earliest_date) live in <name>.toml, never as literal
values in src/vodp's own *code* (grep AC in M9/M21) -- the packaged default
profiles themselves are shipped as ``vodp``'s own package data
(``src/vodp/profiles/*.toml``, see ``[tool.setuptools.package-data]`` in
pyproject.toml) so a real ``pip install .`` (non-editable) can find them
too, not just an editable install run from inside the repo checkout
(R6, Codex/tester review round 2 -- ``profiles/`` used to live at the
repo root, outside the installed package entirely, so a non-editable
install could never find its own default profile).

Lookup order for a profile named ``name``:
  1. ``<VODP_HOME>/profiles/<name>.toml``  (user override)
  2. ``<package_dir>/profiles/<name>.toml`` (packaged default, ships with vodp)

A missing profile is an environment problem, not a bad argument -> exit 5.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from vodp.core.errors import EnvironmentError_
from vodp.core.template import validate_template

PACKAGE_PROFILES_DIR = Path(__file__).resolve().parent.parent / "profiles"


@dataclass
class Profile:
    name: str
    title_template: str = "{title}"
    description_template: str = "{title}\n\n{vod_url}"
    tags: list[str] = field(default_factory=list)
    # No engine-side numeric default (R7, Codex/tester review round 2): a
    # YouTube categoryId is a channel-specific value, same as
    # playlist_name/earliest_date -- it must come from either --category-id
    # or the profile's own [meta] table, never from a literal baked into
    # src/vodp. ``None`` here means "not set by this profile", which
    # settings.py's resolve_settings() turns into a fail-closed
    # EnvironmentError_ (5) if the CLI didn't supply one either, instead
    # of silently uploading with whatever value happened to be hardcoded.
    category_id: str | None = None
    playlist_name: str | None = None
    playlist_id: str | None = None
    playlist_privacy: str = "public"
    earliest_date: str | None = None
    expected_channel_id: str | None = None

    def validate_templates(self) -> None:
        validate_template(self.title_template)
        validate_template(self.description_template)


def verify_expected_channel(expected_channel_id: str | None, actual_channel_id: str) -> None:
    """F5 (Codex/tester review round 2, differed-items follow-up,
    requirements.md 推奨S7): if the profile pins an ``expected_channel_id``,
    every write-type command must confirm the currently authenticated
    account actually *is* that channel before doing anything -- a wrong
    OAuth account (e.g. an operator accidentally authorized vodp against a
    personal channel instead of the intended one) must fail fast (5)
    instead of silently uploading/publishing/deleting on the wrong
    account with nobody watching an unattended run.

    A no-op (never raises) when the profile doesn't set
    ``expected_channel_id`` at all -- this is opt-in, not a default
    behavior change for profiles that never configured it."""
    if expected_channel_id and expected_channel_id != actual_channel_id:
        raise EnvironmentError_(
            f"authenticated YouTube channel {actual_channel_id!r} does not match "
            f"this profile's expected_channel_id {expected_channel_id!r} -- refusing "
            "to run a write-type command against the wrong account"
        )


def _candidate_paths(home: Path, name: str) -> list[Path]:
    return [
        home / "profiles" / f"{name}.toml",
        PACKAGE_PROFILES_DIR / f"{name}.toml",
    ]


def load_profile(home: Path, name: str) -> Profile:
    for path in _candidate_paths(home, name):
        if path.is_file():
            try:
                data = tomllib.loads(path.read_text(encoding="utf-8"))
            except (tomllib.TOMLDecodeError, OSError) as e:
                raise EnvironmentError_(f"profile {path} is unreadable/invalid: {e}") from e
            return _from_dict(name, data)
    raise EnvironmentError_(
        f"profile '{name}' not found in " + ", ".join(str(p) for p in _candidate_paths(home, name))
    )


def _from_dict(name: str, data: dict) -> Profile:
    meta = data.get("meta", {})
    playlist = data.get("playlist", {})
    limits = data.get("limits", {})
    channel = data.get("channel", {})
    profile = Profile(
        name=name,
        title_template=meta.get("title_template", "{title}"),
        description_template=meta.get("description_template", "{title}\n\n{vod_url}"),
        tags=list(meta.get("tags", [])),
        category_id=str(meta["category_id"]) if "category_id" in meta else None,
        playlist_name=playlist.get("name"),
        playlist_id=playlist.get("id"),
        playlist_privacy=playlist.get("privacy", "public"),
        earliest_date=limits.get("earliest_date"),
        expected_channel_id=channel.get("expected_channel_id"),
    )
    profile.validate_templates()
    return profile
