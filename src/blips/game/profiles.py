"""The fields that have habits.

Most of the game's 4,472 airports play from their data alone: the
longest runway lands, a coin decides today's flow, the nearest minor
field with jet pavement becomes the satellite, and a fresh departure
levels at the elevation plus three thousand.  A few fields have
operating personalities the data can't derive — Heathrow sits on the
27s in calm air because the government says so, Tampa deliberately
lands its shorter parallel in south flow — and those live here,
hand-curated, every line defensible against the real operation.  A
field absent from this table plays exactly as generated; every key of
a profile is optional, and an ident the vendored data doesn't carry is
quietly ignored, so the generated sector is always the floor.

Keys, all optional:

``prefer``   the calm-wind flow: a runway-end ident on the longest
             runway.  When the invented surface wind comes up light,
             this end takes the flow instead of the coin toss, and
             flow-change rolls lean back toward it.
``arr``/``dep``  segregated runway assignment by end ident, one entry
             per flow direction, overriding longest-lands (both keys
             or neither — half an assignment is no assignment).
``sat``      the satellite field, pinned by ICAO instead of searched;
             it must still sit inside the sector, and a field the
             vendored data doesn't know falls back to the search.
``initial``  the main field's departure level-off (ft MSL), replacing
             elevation-plus-3,000.  The satellite's thousand-foot LOA
             split is derived *from* whatever this says, so that
             promise holds by construction, profile or none.
``xr``       centre's crossing-restriction menu (ft MSL), replacing
             the generic seven, nine or eleven thousand above the
             field.
"""

PROFILES = {
    # Tampa runs its parallels by the Informal Runway Use Program, not
    # the tape measure: in north flow turbojet arrivals are encouraged
    # onto the long west parallel 1L (1R is noise-sensitive for
    # arrivals), and in south flow that same pavement rolls departures
    # instead — turbojets are encouraged to depart 19R, and 19L is the
    # noise-sensitive one for departures.  So south flow deliberately
    # lands the shorter 8,300-foot parallel, which no longest-lands
    # heuristic could ever derive.
    "KTPA": {
        "arr": ("01L", "19L"),
        "dep": ("01R", "19R"),
    },
    # Portland already finds Brunswick Executive on its own — the pin
    # is documentation that the search agrees with the sectional — and
    # the initial is what the HSKEL departure actually says: maintain
    # three thousand.  A profile that matches the generated sector is
    # the quietest kind of true.
    "KPWM": {
        "sat": "KBXM",
        "initial": 3000.0,
    },
    # Heathrow's westerly preference is government policy, kept since
    # the sixties: with the wind light the 27s hold the flow even
    # against a few knots of easterly, so London hears arrivals, not
    # departures.  Every Heathrow SID caps the climb at 6,000 until
    # further climb comes, and that climb comes in the low flight
    # levels.  Farnborough is the search's own pick, pinned so it
    # stays true.
    "EGLL": {
        "prefer": "27R",
        "sat": "EGLF",
        "initial": 6000.0,
        "xr": (10000.0, 12000.0),
    },
}


def profile_for(icao):
    """The field's own habits — an empty dict for the thousands without."""
    return PROFILES.get(icao, {})
