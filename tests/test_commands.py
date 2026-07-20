"""The command language: terse in, phraseology out, direction always yours."""

import pytest

from blips._commands import (
    CommandError, parse, resolve_callsign, say_altitude, say_digits,
    telephony,
)


def test_chained_transmission():
    query, ins = parse("rpa5655 l 230 c 240 rs 210")
    assert query == "rpa5655"
    assert ins == [
        {"kind": "turn", "hdg": 230, "dir": "l"},
        {"kind": "alt", "alt_ft": 24000, "verb": "c"},
        {"kind": "speed", "kt": 210, "dir": "reduce"},
    ]


def test_altitudes_read_like_data_blocks():
    _, ins = parse("dal1 d 40")
    assert ins[0]["alt_ft"] == 4000          # hundreds of feet, as displayed


def test_full_words_work_too():
    _, ins = parse("dal1 left 90 descend 110 increase 250")
    assert [i["kind"] for i in ins] == ["turn", "alt", "speed"]
    assert ins[0]["dir"] == "l"
    assert ins[2]["dir"] == "increase"


def test_direct_ils_handoff():
    _, ins = parse("55 dct LOOSE i 19l ho")
    assert ins == [
        {"kind": "direct", "fix": "LOOSE"},
        {"kind": "ils", "rwy": "19L"},
        {"kind": "handoff"},
    ]


def test_bare_ils_and_bare_s():
    _, ins = parse("55 i s")
    assert ins == [{"kind": "ils", "rwy": None}, {"kind": "speed", "kt": None}]


def test_via_procedure():
    _, ins = parse("55 via cdogg4")
    assert ins == [{"kind": "procedure", "name": "CDOGG4"}]
    with pytest.raises(CommandError):
        parse("55 via")               # a procedure clearance needs a name


def test_callsign_forgives_a_space():
    assert parse("ual 71 l 230")[0] == "ual71"
    assert parse("ual71 l 230")[0] == "ual71"
    # a suffix hail is digits already; nothing to merge
    assert parse("71 l 230")[0] == "71"


def test_hold_grammar():
    _, ins = parse("55 hold")
    assert ins == [{"kind": "hold", "fix": None}]
    _, ins = parse("55 hold lal")
    assert ins == [{"kind": "hold", "fix": "LAL"}]
    # a following command word is a command, not a fix name
    _, ins = parse("55 hold d 60")
    assert ins[0] == {"kind": "hold", "fix": None}
    assert ins[1]["kind"] == "alt"


def test_no_directionless_shortcuts():
    # holding the picture is the game: no 'fly heading', no bare speed value
    with pytest.raises(CommandError):
        parse("dal1 h 230")
    with pytest.raises(CommandError):
        parse("dal1 s 210")


def test_errors_speak_like_pilots():
    with pytest.raises(CommandError, match="say again"):
        parse("dal1 xyzzy")
    with pytest.raises(CommandError, match="no value"):
        parse("dal1 l")
    with pytest.raises(CommandError, match="out of range"):
        parse("dal1 l 999")
    with pytest.raises(CommandError, match="callsign but no instruction"):
        parse("dal1")


def _acs(*callsigns):
    return [{"callsign": c} for c in callsigns]


def test_callsign_suffix_matching():
    acs = _acs("RPA5655", "DAL455", "SWA1234")
    assert resolve_callsign("rpa5655", acs)["callsign"] == "RPA5655"
    assert resolve_callsign("5655", acs)["callsign"] == "RPA5655"
    assert resolve_callsign("34", acs)["callsign"] == "SWA1234"
    with pytest.raises(CommandError, match="multiple aircraft"):
        resolve_callsign("55", acs)   # RPA5655 and DAL455 both end in 55
    with pytest.raises(CommandError, match="nobody on frequency"):
        resolve_callsign("999", acs)


def test_exact_match_beats_suffix():
    # DAL455 exactly; even though RPA455455 (hypothetically) ends in it
    acs = _acs("DAL455", "RPA1455")
    assert resolve_callsign("DAL455", acs)["callsign"] == "DAL455"


def test_telephony():
    assert telephony("RPA5655") == "Brickyard 5655"
    assert telephony("BAW38W") == "Speedbird 38W"
    # a registration reads phonetically, the way a pilot would say it
    assert telephony("N429SP") == "November four two niner sierra papa"
    assert telephony("GBKLX") == "Golf bravo kilo lima x-ray"
    assert telephony("JA8231") == "Juliett alfa eight two three one"
    assert telephony("RCH412") == "Reach 412"    # the military lifters


def test_radio_numbers():
    assert say_digits(230, 3) == "two three zero"
    assert say_digits(5, 3) == "zero zero five"
    assert say_digits(9) == "niner"
    assert say_altitude(4000) == "four thousand"
    assert say_altitude(11000) == "one one thousand"
    assert say_altitude(4500) == "four thousand five hundred"
    assert say_altitude(24000) == "flight level two four zero"
    assert say_altitude(18000) == "flight level one eight zero"
