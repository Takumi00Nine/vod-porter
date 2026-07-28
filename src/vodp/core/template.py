"""M9/M22 template rendering: {date} {vod_url} {title} {playlist_url}.

Unknown placeholders are a CLI/config content error (exit 2), never a
silent no-op or KeyError leaking to the user.
"""

from __future__ import annotations

import string

from vodp.core.errors import ArgError

ALLOWED_KEYS = frozenset({"date", "vod_url", "title", "playlist_url"})

_formatter = string.Formatter()


class _StrictDict(dict):
    def __missing__(self, key):  # pragma: no cover - guarded by validate() below
        raise ArgError(f"unknown template variable: {{{key}}}")


def validate_template(template: str) -> None:
    """Raise ArgError if ``template`` references an unknown placeholder."""
    for _, field_name, _, _ in _formatter.parse(template):
        if field_name is None:
            continue
        if field_name not in ALLOWED_KEYS:
            raise ArgError(f"unknown template variable: {{{field_name}}}")


def render_template(template: str, values: dict) -> str:
    """Render ``template`` with ``values``. Validates placeholders first."""
    validate_template(template)
    return template.format_map(_StrictDict(values))
