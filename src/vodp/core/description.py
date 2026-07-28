"""M22: description assembly, fixed order per design.md §7:

    render template -> converge {playlist_url}/playlist line to a single
    line -> append protected footer -> normalize (CRLF->LF, strip
    trailing whitespace per line).

``{playlist_url}`` may only appear alone on its own line in a template
(never mixed with other text, never more than once) -- both are template-
authoring errors, not per-run failures, so they raise ArgError (exit 2).
If a template doesn't reference ``{playlist_url}`` at all but a playlist
was actually resolved, the line is still appended automatically as the
final line (before the footer). Everything here is idempotent: calling
twice with the same inputs never grows the description, and finding an
existing-but-different playlist line/footer is a hard conflict (exit 2),
never silently overwritten.
"""

from __future__ import annotations

import re

from vodp.core.errors import ArgError
from vodp.core.template import render_template

_PLAYLIST_SENTINEL = "\x00VODP_PLAYLIST_URL\x00"
PLAYLIST_LABEL = "プレイリスト: "
FOOTER_PREFIX = "[vodp:vod="
PLAYLIST_URL_PREFIX = "https://www.youtube.com/playlist?list="
# ★6: playlist IDs are conservatively estimated at 64 bytes (no official
# hard max is published; this is a safety margin, not an observed limit).
PLAYLIST_ID_MAX_BYTES_ESTIMATE = 64
DESCRIPTION_MAX_BYTES = 5000  # kept in sync with core/preflight.py's constant

_FOOTER_LINE_RE = re.compile(r"^\[vodp:vod=[^\]]+\]$")


def footer_for(twitch_vod_id: str) -> str:
    return f"{FOOTER_PREFIX}{twitch_vod_id}]"


def playlist_url_for(playlist_id: str) -> str:
    return f"{PLAYLIST_URL_PREFIX}{playlist_id}"


def _split_lines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").split("\n")


def normalize(text: str) -> str:
    return "\n".join(line.rstrip() for line in _split_lines(text))


def _find_playlist_line_index(lines: list[str]) -> int | None:
    matches = [i for i, line in enumerate(lines) if line.rstrip().startswith(PLAYLIST_LABEL)]
    if len(matches) > 1:
        raise ArgError(
            f"description already has {len(matches)} playlist lines; refusing to guess which to keep"
        )
    return matches[0] if matches else None


def _append_or_replace_playlist_line(text: str, playlist_url: str | None) -> str:
    """Converge to exactly one ``プレイリスト: <url>`` line when a
    playlist is given; a no-op (leaves any existing line untouched) when
    it isn't -- this function never *removes* a line that isn't ours to
    manage."""
    lines = _split_lines(text)
    idx = _find_playlist_line_index(lines)
    if playlist_url is None:
        return "\n".join(lines)
    target = f"{PLAYLIST_LABEL}{playlist_url}"
    if idx is not None:
        existing = lines[idx].rstrip()
        if existing == target:
            return "\n".join(lines)  # idempotent no-op
        raise ArgError(
            f"description already has a different playlist line ({existing!r} vs {target!r})"
        )
    if lines and lines[-1] == "":
        lines[-1] = target
    else:
        lines.append(target)
    return "\n".join(lines)


def render_with_playlist_placeholder(template: str, values: dict) -> str:
    """Render normally, but substitute ``{playlist_url}`` with an inert
    sentinel first so ``converge_playlist_line`` can validate/replace it
    structurally afterwards instead of it being a plain string already
    baked into the text."""
    values = dict(values)
    values["playlist_url"] = _PLAYLIST_SENTINEL
    return render_template(template, values)


def converge_playlist_line(rendered: str, playlist_url: str | None) -> str:
    """Handles the case where the *template* referenced ``{playlist_url}``
    (now the sentinel, post-render). If it didn't, falls back to the
    generic append/replace-by-label convergence. A template that has
    *both* the ``{playlist_url}`` placeholder and a separately hardcoded
    ``プレイリスト: ...`` line is a template-authoring conflict (would
    otherwise silently produce two lines) -- fail closed."""
    lines = _split_lines(rendered)
    sentinel_indexes = [i for i, line in enumerate(lines) if _PLAYLIST_SENTINEL in line]
    label_indexes = [
        i for i, line in enumerate(lines)
        if line.rstrip().startswith(PLAYLIST_LABEL) and _PLAYLIST_SENTINEL not in line
    ]

    if len(sentinel_indexes) > 1:
        raise ArgError("{playlist_url} may only appear once in a description template")
    if sentinel_indexes and label_indexes:
        raise ArgError(
            "description has both a {playlist_url} placeholder and a literal "
            "playlist line; use only one"
        )

    if not sentinel_indexes:
        return _append_or_replace_playlist_line("\n".join(lines), playlist_url)

    idx = sentinel_indexes[0]
    if lines[idx].strip() != _PLAYLIST_SENTINEL:
        raise ArgError("{playlist_url} must be alone on its own line in the description template")
    if playlist_url:
        lines[idx] = f"{PLAYLIST_LABEL}{playlist_url}"
    else:
        del lines[idx]
    return "\n".join(lines)


def apply_footer(text: str, twitch_vod_id: str) -> str:
    """Append/verify the protected footer. Idempotent for the same
    vod_id (and moved to the last line if it wasn't already there);
    refuses (ArgError) to silently reassign an existing footer *anywhere
    in the text* to a different vod_id -- checking only the last line
    would miss a conflicting footer left mid-file by a hand-edited
    --description-file (Codex review, major #5)."""
    lines = [line.rstrip() for line in _split_lines(text)]
    while lines and lines[-1] == "":
        lines.pop()
    target = footer_for(twitch_vod_id)
    footer_indexes = [i for i, line in enumerate(lines) if _FOOTER_LINE_RE.match(line)]
    if len(footer_indexes) > 1:
        raise ArgError(f"description already has {len(footer_indexes)} vodp footer lines")

    if footer_indexes:
        idx = footer_indexes[0]
        if lines[idx] != target:
            raise ArgError(
                f"description already carries a footer for a different VOD ({lines[idx]!r} vs {target!r})"
            )
        if idx == len(lines) - 1:
            return "\n".join(lines)  # already correct and already last -- idempotent no-op
        del lines[idx]  # same id but not last (e.g. hand-edited) -- move it to the end
        lines.append(target)
        return "\n".join(lines)

    lines.append(target)
    return "\n".join(lines)


def build_description_for_insert(template: str, values: dict, *, twitch_vod_id: str, playlist_url: str | None) -> str:
    """Fresh-insert path: template -> playlist line -> footer -> normalize."""
    rendered = render_with_playlist_placeholder(template, values)
    converged = converge_playlist_line(rendered, playlist_url)
    return normalize(apply_footer(converged, twitch_vod_id))


def build_description_for_meta_set(current_text: str, *, twitch_vod_id: str, playlist_url: str | None) -> str:
    """`meta set` path: re-run the same convergence/footer protection
    against whatever description text is being written (current YouTube
    snippet, or --description-file content) instead of a template render."""
    converged = _append_or_replace_playlist_line(current_text, playlist_url)
    return normalize(apply_footer(converged, twitch_vod_id))


def converge_playlist_line_only(text: str, playlist_url: str | None) -> str:
    """Same playlist-line convergence as ``build_description_for_meta_set``,
    without touching the footer -- for the case where there's no known
    twitch_vod_id to protect/re-apply a footer for (e.g. `meta set` on a
    video with no existing vodp footer), so a bare ``--playlist-id`` isn't
    silently dropped just because footer protection has nothing to anchor
    to (Codex review, major #6)."""
    return normalize(_append_or_replace_playlist_line(text, playlist_url))


def estimate_new_playlist_line_bytes() -> int:
    """Worst-case byte length of the playlist line alone, for callers
    that just want the margin number. Prefer
    ``build_description_preview_for_new_playlist`` + ``check_new_playlist_description_budget``
    together -- checking length in isolation like this misses a
    pre-existing conflicting playlist line (Codex review, critical #2)."""
    sample = f"{PLAYLIST_LABEL}{PLAYLIST_URL_PREFIX}{'X' * PLAYLIST_ID_MAX_BYTES_ESTIMATE}"
    return len(sample.encode("utf-8")) + 1  # +1 for the newline joining it


def _worst_case_placeholder_url() -> str:
    return f"{PLAYLIST_URL_PREFIX}{'X' * PLAYLIST_ID_MAX_BYTES_ESTIMATE}"


def build_description_preview_for_new_playlist(template: str, values: dict, *, twitch_vod_id: str) -> str:
    """Preview using a worst-case-length *placeholder* playlist URL
    (instead of ``None``), so this one call both (a) yields an exact
    byte-length estimate for the not-yet-created playlist and (b) runs
    the real single-line/conflict validation -- previewing with
    ``playlist_url=None`` would silently skip validating against a
    pre-existing (possibly different) playlist line, which would then
    only surface as a conflict *after* the real playlist was already
    created (Codex review, critical #2)."""
    return build_description_for_insert(
        template, values, twitch_vod_id=twitch_vod_id, playlist_url=_worst_case_placeholder_url(),
    )


def check_new_playlist_description_budget(preview_description_with_placeholder_url: str) -> None:
    """M22 orphan-playlist prevention: before *creating* a new playlist
    (whose final ID length isn't known yet), verify the description --
    already built via ``build_description_preview_for_new_playlist`` --
    fits under 5000 bytes. Raises ArgError (exit 2) instead of creating an
    orphaned playlist that no description ends up referencing; the caller
    should pass --playlist-id for an existing playlist, or create the
    playlist manually instead."""
    if len(preview_description_with_placeholder_url.encode("utf-8")) > DESCRIPTION_MAX_BYTES:
        raise ArgError(
            "description is too close to the 5000-byte limit to safely auto-create a new "
            "playlist (its final ID length isn't known yet); pass --playlist-id for an "
            "existing playlist, or create the playlist manually first"
        )
