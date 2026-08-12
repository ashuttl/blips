"""The voice module: how a radio line becomes speech, and who speaks it.

None of this plays audio — it exercises the text transform and the casting
logic, both pure. The Speaker's worker thread and macOS `say` calls stay out
of the tests.
"""

from blips.game.voice import Speaker, _spoken_text


def test_flight_numbers_are_spoken_as_digits():
    # altitudes and headings arrive already spelled out; only the callsign's
    # number is bare, and "5655" must not read as "five thousand six hundred…"
    assert (_spoken_text("Brickyard 5655, turn left heading two three zero")
            == "Brickyard five six five five, turn left heading two three zero")
    # a lone digit is left alone; zeros are preserved
    assert _spoken_text("gate 7") == "gate 7"
    assert _spoken_text("squawk 1200") == "squawk one two zero zero"


def _make(voices):
    """A Speaker with its installed-voice set injected, so casting is tested
    against a known lineup on any machine — no worker thread, no `say`."""
    sp = object.__new__(Speaker)
    sp._voices = voices
    flat = sorted({v for names in voices.values() for v in names})
    sp._default = voices.get("en_US", [None])[0] or (flat[0] if flat else None)
    sp._assigned = {}
    return sp


FULL = {
    "en_US": ["Samantha", "Ava", "Fred"], "en_GB": ["Daniel"],
    "en_IE": ["Moira"], "en_IN": ["Rishi"], "en_AU": ["Karen"],
}


def test_accent_follows_the_airline():
    sp = _make(FULL)
    assert sp._voice_for("BAW451") == "Daniel"    # Speedbird → UK
    assert sp._voice_for("EIN23") == "Moira"      # Shamrock → Ireland
    assert sp._voice_for("AIC101") == "Rishi"     # Air India
    assert sp._voice_for("QFA12") == "Karen"      # Qantas → Australia
    # an unlisted airline lands in the neutral US pool
    assert sp._voice_for("UAL889") in FULL["en_US"]


def test_a_flight_keeps_its_voice():
    sp = _make(FULL)
    assert sp._voice_for("DAL500") == sp._voice_for("DAL500")


def test_atis_has_its_own_voice():
    sp = _make(FULL)
    assert sp._voice_for("ATIS") in ("Fred", "Daniel", "Samantha")


def test_missing_accent_falls_back_not_crashes():
    # a machine with only the default US voices still casts everyone
    sp = _make({"en_US": ["Samantha"]})
    assert sp._voice_for("BAW451") == "Samantha"
    assert sp._voice_for("QFA12") == "Samantha"
    assert sp._voice_for("ATIS") == "Samantha"


def test_no_voices_at_all_is_survivable():
    sp = _make({})
    assert sp._voice_for("DAL500") is None        # speak with the system default
    assert sp._voice_for("ATIS") is None


# --- the piper company -----------------------------------------------------

from blips.game.voice import _ACCENT, _PIPER_MODELS, _PiperBackend


def test_piper_pools_cover_every_accent_bucket():
    # every accent an airline can ask for resolves to a real, non-empty pool
    voices = _PiperBackend.voices()
    for locale in set(_ACCENT.values()) | {"en_US"}:
        assert voices[locale], locale
    # and every token is a (known model, in-range speaker id)
    for pool in voices.values():
        for model, sid in pool:
            assert 0 <= sid < _PIPER_MODELS[model]


def test_piper_casting_splits_the_atlantic():
    sp = _make(_PiperBackend.voices())
    assert sp._voice_for("BAW451")[0] == "en_GB-vctk-medium"   # Speedbird
    assert sp._voice_for("EIN23")[0] == "en_GB-vctk-medium"    # Shamrock
    assert sp._voice_for("UAL889")[0] == "en_US-libritts_r-medium"
    # a flight keeps its voice here too
    assert sp._voice_for("BAW451") == sp._voice_for("BAW451")


def test_piper_atis_is_fixed():
    sp = _make(_PiperBackend.voices())
    sp._atis_pref = _PiperBackend.atis_pref
    assert sp._voice_for("ATIS") == _PiperBackend.atis_pref[0]
