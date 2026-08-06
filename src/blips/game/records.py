"""The shift book: personal records per airport, kept with the caches.

Nothing here touches play — it's the book you check afterwards.  One
JSON file under ~/.cache/blips holds, per airport, the lifetime tallies
and the best rated shift so far, and the shift card compares you to
yourself: deterministic sectors make airports learnable, this writes
the learning down.
"""

import json
import time

from blips._cache import CACHE_ROOT

PATH = CACHE_ROOT / "records.json"


def load():
    try:
        return json.loads(PATH.read_text())
    except Exception:
        return {}


def first_shift():
    """True while no page in the book carries a rated best — the mark of
    a player who has never finished a real shift anywhere.  The game
    reads this once, at the top of a shift, to decide whether to open
    gently."""
    return not any((entry or {}).get("best")
                   for entry in load().values())


def record_shift(icao, *, score, rating, minutes, landed, handed, busts,
                 offered=0):
    """Fold one shift into the book; returns (entry, previous_best).

    The best is the best *rate* — score against what the concluded
    traffic was worth — because that's how the shift itself is graded:
    a short brilliant hour outranks a long mediocre one.  A shift too
    short to earn a rating still tallies, but can't set a personal
    best — a lucky two minutes isn't a record.  A best written before
    the book graded by rate has no ratio; any rated shift retires it.
    """
    book = load()
    entry = book.setdefault(icao, {
        "shifts": 0, "landed": 0, "handed": 0, "busts": 0, "best": None})
    prev = entry.get("best")
    entry["shifts"] += 1
    entry["landed"] += landed
    entry["handed"] += handed
    entry["busts"] += busts
    ratio = score / offered if offered else 0.0
    if rating != "—" and (prev is None or "ratio" not in prev
                          or ratio > prev["ratio"]):
        entry["best"] = {"score": score, "rating": rating,
                         "ratio": round(ratio, 3), "minutes": minutes,
                         "when": int(time.time())}
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        PATH.write_text(json.dumps(book))
    except OSError:
        pass                     # a read-only home never blocks the game
    return entry, prev
