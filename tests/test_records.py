"""The shift book: tallies always, personal bests only when earned."""

import json

from blips.game import records as _records


def _shift(**kw):
    base = dict(score=1000, rating="B", minutes=10,
                landed=5, handed=3, busts=0, offered=1200)
    base.update(kw)
    return base


def test_first_shift_opens_the_book(tmp_path, monkeypatch):
    monkeypatch.setattr(_records, "PATH", tmp_path / "records.json")
    entry, prev = _records.record_shift("KTPA", **_shift())
    assert prev is None
    assert entry["best"]["score"] == 1000
    assert entry["shifts"] == 1 and entry["landed"] == 5


def test_tallies_accumulate_but_a_worse_shift_keeps_the_best(tmp_path,
                                                             monkeypatch):
    monkeypatch.setattr(_records, "PATH", tmp_path / "records.json")
    _records.record_shift("KTPA", **_shift())
    entry, prev = _records.record_shift(
        "KTPA", **_shift(score=800, rating="A", landed=4, busts=1))
    assert prev["score"] == 1000
    assert entry["best"]["score"] == 1000     # 800 didn't beat it
    assert entry["shifts"] == 2
    assert entry["landed"] == 9
    assert entry["busts"] == 1


def test_a_better_shift_takes_the_record(tmp_path, monkeypatch):
    monkeypatch.setattr(_records, "PATH", tmp_path / "records.json")
    _records.record_shift("KTPA", **_shift())
    entry, prev = _records.record_shift("KTPA", **_shift(score=1500,
                                                         rating="A"))
    assert prev["score"] == 1000              # what the card compares to
    assert entry["best"]["score"] == 1500


def test_unrated_blinks_tally_but_never_set_records(tmp_path, monkeypatch):
    monkeypatch.setattr(_records, "PATH", tmp_path / "records.json")
    _records.record_shift("KTPA", **_shift())
    entry, _prev = _records.record_shift(
        "KTPA", **_shift(score=9999, rating="—", minutes=1))
    assert entry["best"]["score"] == 1000     # a lucky blink isn't a record
    assert entry["shifts"] == 2


def test_the_best_is_a_rate_not_an_endurance_run(tmp_path, monkeypatch):
    monkeypatch.setattr(_records, "PATH", tmp_path / "records.json")
    _records.record_shift("KTPA", **_shift(score=5000, offered=10000,
                                           rating="C", minutes=90))
    entry, prev = _records.record_shift(
        "KTPA", **_shift(score=900, offered=1000, rating="A", minutes=12))
    assert prev["score"] == 5000
    assert entry["best"]["score"] == 900   # 90% beats 50%, however long
    assert entry["best"]["ratio"] == 0.9
    assert entry["best"]["minutes"] == 12


def test_a_legacy_best_yields_to_any_rated_shift(tmp_path, monkeypatch):
    # a best written before the book graded by rate carries no ratio —
    # it can't be compared, so the first rated shift retires it
    path = tmp_path / "records.json"
    monkeypatch.setattr(_records, "PATH", path)
    path.write_text(json.dumps({"KTPA": {
        "shifts": 40, "landed": 300, "handed": 200, "busts": 9,
        "best": {"score": 99999, "rating": "B", "minutes": 480,
                 "when": 0}}}))
    entry, prev = _records.record_shift(
        "KTPA", **_shift(score=400, offered=800, rating="C"))
    assert "ratio" not in prev
    assert entry["best"]["score"] == 400
    assert entry["best"]["ratio"] == 0.5


def test_airports_keep_separate_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(_records, "PATH", tmp_path / "records.json")
    _records.record_shift("KTPA", **_shift())
    entry, prev = _records.record_shift("EGLL", **_shift(score=700))
    assert prev is None
    assert entry["best"]["score"] == 700
    assert _records.load()["KTPA"]["best"]["score"] == 1000
