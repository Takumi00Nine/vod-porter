"""M9 AC: placeholder replacement + unknown key -> exit 2 (ArgError)."""

from __future__ import annotations

import pytest

from vodp.core.errors import ArgError
from vodp.core.template import render_template, validate_template


def test_all_known_placeholders_replaced():
    out = render_template(
        "{date} {title} {vod_url} {playlist_url}",
        {"date": "2026-07-27", "title": "T", "vod_url": "https://v", "playlist_url": "https://p"},
    )
    assert out == "2026-07-27 T https://v https://p"


def test_unknown_placeholder_raises_arg_error():
    with pytest.raises(ArgError):
        render_template("{unknown_var}", {})


def test_validate_template_accepts_empty_braces_literal():
    validate_template("no placeholders here")


def test_literal_braces_are_not_flagged():
    # str.Formatter treats "{{" / "}}" as escaped literal braces
    validate_template("literal {{brace}} stays")
