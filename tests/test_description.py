"""M22 AC: {playlist_url} single-line-only, idempotent convergence,
footer protection, orphan-playlist byte-budget check."""

from __future__ import annotations

import pytest

from vodp.core.description import (
    apply_footer,
    build_description_for_insert,
    build_description_for_meta_set,
    build_description_preview_for_new_playlist,
    check_new_playlist_description_budget,
    footer_for,
)
from vodp.core.errors import ArgError


# -- {playlist_url} single-line-only ----------------------------------------

def test_playlist_url_placeholder_replaced_on_its_own_line():
    out = build_description_for_insert(
        "Body\n{playlist_url}\nMore",
        {"date": "d", "vod_url": "u", "title": "t"},
        twitch_vod_id="v1", playlist_url="https://y/list?list=PL1",
    )
    assert "プレイリスト: https://y/list?list=PL1" in out.splitlines()


def test_playlist_url_placeholder_mixed_with_text_raises():
    with pytest.raises(ArgError):
        build_description_for_insert(
            "see {playlist_url} here", {"date": "d", "vod_url": "u", "title": "t"},
            twitch_vod_id="v1", playlist_url="https://x",
        )


def test_playlist_url_placeholder_twice_raises():
    with pytest.raises(ArgError):
        build_description_for_insert(
            "{playlist_url}\n{playlist_url}", {"date": "d", "vod_url": "u", "title": "t"},
            twitch_vod_id="v1", playlist_url="https://x",
        )


def test_no_placeholder_but_playlist_given_appends_final_line():
    out = build_description_for_insert(
        "Body only", {"date": "d", "vod_url": "u", "title": "t"},
        twitch_vod_id="v1", playlist_url="https://y/list?list=PL1",
    )
    lines = out.splitlines()
    assert lines[-2] == "プレイリスト: https://y/list?list=PL1"  # footer is the actual last line


def test_no_placeholder_no_playlist_no_line_added():
    out = build_description_for_insert(
        "Body only", {"date": "d", "vod_url": "u", "title": "t"},
        twitch_vod_id="v1", playlist_url=None,
    )
    assert "プレイリスト:" not in out


# -- idempotency --------------------------------------------------------------

def test_applying_twice_is_idempotent():
    once = build_description_for_insert(
        "Body", {"date": "d", "vod_url": "u", "title": "t"},
        twitch_vod_id="v1", playlist_url="https://y/list?list=PL1",
    )
    twice = build_description_for_meta_set(once, twitch_vod_id="v1", playlist_url="https://y/list?list=PL1")
    assert once == twice


def test_different_existing_playlist_line_conflicts():
    text = "Body\nプレイリスト: https://y/list?list=OLD"
    with pytest.raises(ArgError):
        build_description_for_meta_set(text, twitch_vod_id="v1", playlist_url="https://y/list?list=NEW")


def test_meta_set_no_playlist_change_leaves_existing_line_alone():
    text = "Body\nプレイリスト: https://y/list?list=OLD"
    out = build_description_for_meta_set(text, twitch_vod_id="v1", playlist_url=None)
    assert "プレイリスト: https://y/list?list=OLD" in out


def test_placeholder_and_literal_playlist_line_both_present_raises():
    """Codex review, major #4: a template with both {playlist_url} and a
    separately hardcoded playlist line must not silently produce two
    lines -- it's a template-authoring conflict."""
    template = "Body\n{playlist_url}\nプレイリスト: https://y/list?list=OLD"
    with pytest.raises(ArgError):
        build_description_for_insert(
            template, {"date": "d", "vod_url": "u", "title": "t"},
            twitch_vod_id="v1", playlist_url="https://y/list?list=NEW",
        )


# -- footer protection ---------------------------------------------------------

def test_footer_appended_when_absent():
    out = apply_footer("Body", "v1")
    assert out.splitlines()[-1] == footer_for("v1")


def test_footer_idempotent_same_vod():
    once = apply_footer("Body", "v1")
    twice = apply_footer(once, "v1")
    assert once == twice


def test_footer_different_vod_id_raises():
    once = apply_footer("Body", "v1")
    with pytest.raises(ArgError):
        apply_footer(once, "v2")


def test_footer_conflict_detected_mid_file_not_just_last_line():
    """Codex review, major #5: a stray footer left mid-file (e.g. by a
    hand-edited --description-file) must still be detected as a conflict,
    not just when it happens to be the last line."""
    text = "Body\n[vodp:vod=other]\nMore text after it"
    with pytest.raises(ArgError):
        apply_footer(text, "v1")


def test_footer_same_vod_mid_file_moved_to_last_line():
    text = "Body\n[vodp:vod=v1]\nMore text after it"
    out = apply_footer(text, "v1")
    assert out.splitlines()[-1] == footer_for("v1")
    assert out.count("[vodp:vod=v1]") == 1


# -- CRLF / trailing whitespace normalization ----------------------------------

def test_crlf_and_trailing_whitespace_normalized():
    out = build_description_for_insert(
        "Line1  \r\nLine2\t\r\n", {"date": "d", "vod_url": "u", "title": "t"},
        twitch_vod_id="v1", playlist_url=None,
    )
    assert "\r" not in out
    assert all(not line.endswith((" ", "\t")) for line in out.splitlines())


# -- orphan playlist byte budget ------------------------------------------------

def test_new_playlist_budget_ok_when_room():
    check_new_playlist_description_budget("short description")


def test_new_playlist_budget_exceeded_raises():
    huge = "x" * 5001
    with pytest.raises(ArgError):
        check_new_playlist_description_budget(huge)


def test_new_playlist_budget_boundary():
    exactly_fitting = "x" * 5000
    check_new_playlist_description_budget(exactly_fitting)  # exactly at the limit: OK
    with pytest.raises(ArgError):
        check_new_playlist_description_budget(exactly_fitting + "x")  # one byte over: fails


def test_new_playlist_preview_catches_multibyte_title_underestimate():
    """Codex review, critical #2: a short *character* count title can
    still blow the byte budget once it's multi-byte UTF-8 (e.g. emoji) --
    the real preview builder must catch this, not a naive char-length
    proxy."""
    template = "{title}\n\n" + ("x" * 4700)
    ascii_preview = build_description_preview_for_new_playlist(
        template, {"date": "d", "vod_url": "u", "title": "a" * 90}, twitch_vod_id="v1",
    )
    check_new_playlist_description_budget(ascii_preview)  # fits: ASCII title is cheap

    emoji_preview = build_description_preview_for_new_playlist(
        template, {"date": "d", "vod_url": "u", "title": "\U0001F600" * 90}, twitch_vod_id="v1",
    )
    with pytest.raises(ArgError):
        check_new_playlist_description_budget(emoji_preview)  # 90 emoji = 360 bytes, blows the budget


def test_new_playlist_preview_catches_preexisting_conflicting_line():
    """Codex review, critical #2: a description that already carries a
    *different* playlist line must be caught by the preview step, before
    ever calling playlists.insert -- not only after, when building the
    real final description."""
    template = "Body\nプレイリスト: https://y/list?list=OLD"
    with pytest.raises(ArgError):
        build_description_preview_for_new_playlist(
            template, {"date": "d", "vod_url": "u", "title": "t"}, twitch_vod_id="v1",
        )
