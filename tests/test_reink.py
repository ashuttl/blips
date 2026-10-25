"""A theme change mid-session: the terminal's OSC answer arrives as input
and every copy of the derived palette follows it."""

import os

import pytest

import blips._app as app_mod  # noqa: F401 — holds a copy of CHIP_BG
import blips._basemap as basemap
import blips._color as color
import blips._keys as keys
import blips._theme as theme
import blips.scope as scope


@pytest.fixture
def restore():
    saved = (theme.theme_fg, theme.theme_bg, theme.theme_available)
    yield
    theme.theme_fg, theme.theme_bg, theme.theme_available = saved
    scope.reink()


def test_osc_reply_reads_as_one_action():
    r, w = os.pipe()
    try:
        os.write(w, b"\033]11;rgb:f0f0/eded/e4e4\033\\q")
        assert keys.read_key(r) == "osc:11;rgb:f0f0/eded/e4e4"
        assert keys.read_key(r) == "quit"      # nothing leaked as keys
        os.write(w, b"\033]10;rgb:1010/2020/3030\007")
        assert keys.read_key(r) == "osc:10;rgb:1010/2020/3030"
    finally:
        os.close(r)
        os.close(w)


def test_new_background_reinks_every_copy(restore):
    assert theme.ingest_osc("11;rgb:f0f0/eded/e4e4")
    light = (240, 237, 228)
    assert theme.theme_bg == light
    assert color.BG_PRIMARY == light and scope.BG_PRIMARY == light
    assert basemap.SEA_FILL == scope.SEA_FILL == basemap.sea_fill(light)
    assert app_mod.CHIP_BG == scope.CHIP_BG
    # the same answer again changes nothing and says so
    assert not theme.ingest_osc("11;rgb:f0f0/eded/e4e4")


def test_other_replies_are_ignored(restore):
    before = theme.theme_bg
    assert not theme.ingest_osc("4;1;rgb:ffff/0000/0000")
    assert not theme.ingest_osc("11;garbage")
    assert theme.theme_bg == before
