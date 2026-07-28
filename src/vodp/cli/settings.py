"""M2 option resolution: CLI > profile > built-in default.

argparse leaves every optional flag at ``None`` unless the user passed it
explicitly (see parser.py -- none of the meta/limit options set a
``default`` other than ``max_uploads``/``playlist privacy``, which are
handled the same way so the priority chain stays uniform and testable).
"""

from __future__ import annotations

from dataclasses import dataclass

from vodp.core.errors import ArgError, EnvironmentError_
from vodp.core.profile import Profile

# No "category_id" entry here (R7, Codex/tester review round 2): a
# YouTube categoryId is a channel-specific value like playlist name/tags,
# not an engine constant -- it must come from --category-id or the
# profile's own [meta] table, never a literal baked into src/vodp. See
# resolve_settings() below for the fail-closed EnvironmentError_ if
# neither source provides one.
BUILTIN_DEFAULTS = {
    "privacy": "private",
    "playlist": None,
    "playlist_id": None,
    "playlist_privacy": "public",
    "tags": "",
    "max_uploads": 5,
    "deadline_seconds": 3600.0,
}


@dataclass
class Settings:
    title: str | None
    description_file: str | None
    privacy: str
    playlist: str | None
    playlist_id: str | None
    playlist_privacy: str
    tags: list[str]
    category_id: str
    max_uploads: int
    deadline_seconds: float
    allow_empty: bool
    dry_run: bool
    non_interactive: bool
    profile: Profile


def _first_not_none(*values):
    for v in values:
        if v is not None:
            return v
    return None


def resolve_settings(args, profile: Profile) -> Settings:
    """Build a Settings object applying CLI > profile > builtin priority."""
    privacy = _first_not_none(
        getattr(args, "privacy", None), BUILTIN_DEFAULTS["privacy"]
    )
    playlist = _first_not_none(
        getattr(args, "playlist", None), profile.playlist_name, BUILTIN_DEFAULTS["playlist"]
    )
    playlist_id = _first_not_none(
        getattr(args, "playlist_id", None), profile.playlist_id, BUILTIN_DEFAULTS["playlist_id"]
    )
    playlist_privacy = _first_not_none(
        getattr(args, "playlist_privacy", None), profile.playlist_privacy, BUILTIN_DEFAULTS["playlist_privacy"]
    )
    tags_arg = getattr(args, "tags", None)
    if tags_arg is not None:
        tags = [t.strip() for t in tags_arg.split(",") if t.strip()]
    else:
        tags = list(profile.tags)
    # R7 minor follow-up (Codex light review, round-2): `_first_not_none`
    # treats "" as a perfectly valid value, so an empty/whitespace-only
    # --category-id or profile [meta] category_id used to sail straight
    # through to insert_video's body instead of being caught by the
    # None-check below -- check each source individually (with its own
    # matching exit code: a bad CLI flag is 2, a bad profile value is 5)
    # *before* combining them.
    cli_category_id = getattr(args, "category_id", None)
    if cli_category_id is not None and not cli_category_id.strip():
        raise ArgError("--category-id must not be empty/whitespace-only")
    profile_category_id = profile.category_id
    if profile_category_id is not None and not profile_category_id.strip():
        raise EnvironmentError_("profile's [meta] category_id must not be empty/whitespace-only")
    category_id = _first_not_none(cli_category_id, profile_category_id)
    if category_id is None:
        raise EnvironmentError_(
            "no YouTube categoryId configured -- pass --category-id or set "
            "[meta] category_id in the profile (channel-specific value, not a "
            "src/vodp default)"
        )
    max_uploads = getattr(args, "max_uploads", None)
    if max_uploads is None:
        max_uploads = BUILTIN_DEFAULTS["max_uploads"]
    if not isinstance(max_uploads, int) or max_uploads <= 0:
        raise ArgError(f"--max-uploads must be a positive integer, got {max_uploads!r}")

    deadline_seconds = getattr(args, "deadline", None)
    if deadline_seconds is None:
        deadline_seconds = BUILTIN_DEFAULTS["deadline_seconds"]
    if not isinstance(deadline_seconds, (int, float)) or deadline_seconds <= 0:
        raise ArgError(f"--deadline must be a positive number of seconds, got {deadline_seconds!r}")

    return Settings(
        title=getattr(args, "title", None),
        description_file=getattr(args, "description_file", None),
        privacy=privacy,
        playlist=playlist,
        playlist_id=playlist_id,
        playlist_privacy=playlist_privacy,
        tags=tags,
        category_id=str(category_id),
        max_uploads=max_uploads,
        deadline_seconds=float(deadline_seconds),
        allow_empty=bool(getattr(args, "allow_empty", False)),
        dry_run=bool(getattr(args, "dry_run", False)),
        non_interactive=bool(getattr(args, "non_interactive", False)),
        profile=profile,
    )
