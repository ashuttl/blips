"""The help card and the fitted bar hint: nothing clips, everything shows.

The #1 discoverability gap was a 140-column hint on an 80-column world —
players never saw `i` (the win condition), `hold`, `tfc`, `ho`, or the
`? help` affordance itself.  The card and the wrap are pure functions, so
what they promise is checked here without a terminal.
"""

from blips.game.app import _Console, fit_hint, help_lines
from blips.scope import _footer_alpha


def _text(row):
    return "".join(seg[0] for seg in row)


# -- the card --------------------------------------------------------------

def test_page_one_covers_the_radio():
    text = "\n".join(_text(r) for r in help_lines(1))
    for verb in ("l 230 / r 230", "c 110 / d 40", "rs 180 / is 250",
                 "dct LAL", "via CDOGG4", "hold / hold LAL", "i / i",
                 "tfc", "ho"):
        assert verb in text
    assert "hundreds of feet" in text          # the rule, stated plainly
    assert "240 = FL240" in text


def test_page_one_covers_the_desk_and_the_mouse():
    text = "\n".join(_text(r) for r in help_lines(1))
    for key in ("^O procedures", "^B labels", "^V voice", "^L log",
                "^W weather", "^P pause", "zoom", "q quit", "? this card"):
        assert key in text
    assert "click a blip" in text
    assert "hover" in text and "corner post" in text


def test_worked_example_uses_the_live_callsign():
    text = "\n".join(_text(r) for r in help_lines(1, example="RPA5655",
                                                  rwy="9"))
    assert "rpa5655 l 230 d 60" in text        # a flight you could key now
    assert "i / i 9" in text                   # today's runway, not a stock one
    # and a sector with nobody on frequency still gets an example
    assert "dal204 l 230 d 60" in "\n".join(_text(r) for r in help_lines(1))


def test_page_two_is_the_glossary():
    text = "\n".join(_text(r) for r in help_lines(2))
    for word in ("established", "localizer", "par", "corner post",
                 "scratchpad", "hearback", "NORDO", "flow change",
                 "the push", "MVA"):
        assert word in text


def test_card_lines_never_clip():
    # under 78 columns, every line, both pages, even with a long callsign
    for page in (1, 2):
        for row in help_lines(page, example="N8342QB", rwy="27R"):
            assert len(_text(row)) < 78, _text(row)


# -- the fitted hint -------------------------------------------------------

def test_hint_fits_any_width():
    for width in (60, 72, 80, 100, 132, 200):
        rows = fit_hint(width)
        for row in rows:
            assert len(_text(row)) <= width, (width, _text(row))
        # the way in is always on screen, and always on the bar row
        assert "? help" in _text(rows[-1])
        joined = " ".join(_text(r) for r in rows)
        for verb in ("hold [FIX]", "i [rwy]", "tfc", "ho handoff", "^L log"):
            assert verb in joined


def test_hint_is_one_row_on_a_wide_terminal():
    rows = fit_hint(160)
    assert len(rows) == 1
    assert _text(rows[0]).startswith("▸ ")
    assert len(fit_hint(80)) > 1               # and it wraps where it must


def test_footer_fade_keys_on_distance_from_bottom():
    # a four-row footer (the hint wrapped on a narrow terminal) stays legible
    assert all(_footer_alpha(i, 4) == 1.0 for i in range(4))
    # while the ten-row tape still dissolves upward, oldest faintest
    assert _footer_alpha(9, 10) == 1.0
    assert _footer_alpha(0, 10) < 0.2


# -- the console -----------------------------------------------------------

class _StubSim:
    radio = []
    aircraft = []
    sector = {"rwy": "19L"}


def test_help_card_takes_over_the_footer():
    console = _Console(_StubSim(), lambda word: False)
    console.help_page = 1
    rows = console.footer(None)
    text = "\n".join(_text(segs) for segs, _alpha in rows)
    assert "help — the radio" in text
    assert rows[0][1] is None                  # pinned: the card never fades
    assert "▸ " in text                        # the bar still rides underneath
    console.help_page = 2
    assert "hearback" in "\n".join(
        _text(segs) for segs, _a in console.footer(None))


def test_tape_opens_by_default():
    # GAME.md: the tape starts open — learning the frequency means reading it
    assert _Console(_StubSim(), lambda word: False).log_open
