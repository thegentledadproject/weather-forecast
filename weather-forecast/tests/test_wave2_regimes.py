"""
regimes.py: split any row list at config.REGIME_BOUNDARIES so a report can
print each side of a wave's deploy date separately (spec Principle 3).
"""
from datetime import date

import pytest

import config
import regimes

B1 = date(2026, 9, 21)
B2 = date(2026, 10, 5)


def _rows(*days):
    return [{"target_date": d, "i": i} for i, d in enumerate(days)]


def test_no_boundaries_is_one_segment_called_all(monkeypatch):
    monkeypatch.setattr(config, "REGIME_BOUNDARIES", ())
    rows = _rows(date(2026, 9, 1), date(2026, 9, 30))
    assert regimes.boundaries() == ()
    assert regimes.segment_labels(()) == ["all"]
    assert regimes.regime_segments(rows) == [("all", rows)]


def test_one_boundary_splits_before_and_from(monkeypatch):
    monkeypatch.setattr(config, "REGIME_BOUNDARIES", ("2026-09-21",))
    rows = _rows(date(2026, 9, 20), date(2026, 9, 21), date(2026, 9, 22))
    assert regimes.boundaries() == (B1,)
    assert regimes.regime_segments(rows) == [
        ("pre-2026-09-21", rows[:1]),
        ("from-2026-09-21", rows[1:]),
    ]


def test_the_boundary_day_belongs_to_the_new_regime():
    assert regimes.segment_index(B1, (B1,)) == 1
    assert regimes.segment_index(date(2026, 9, 20), (B1,)) == 0


def test_two_boundaries_make_three_segments_and_keep_empty_ones():
    bounds = (B1, B2)
    assert regimes.segment_labels(bounds) == [
        "pre-2026-09-21", "2026-09-21..2026-10-04", "from-2026-10-05",
    ]
    rows = _rows(date(2026, 9, 25))
    assert regimes.regime_segments(rows, bounds=bounds) == [
        ("pre-2026-09-21", []),
        ("2026-09-21..2026-10-04", rows),
        ("from-2026-10-05", []),
    ]


def test_the_key_is_pluggable():
    class Entry:
        def __init__(self, d):
            self.target_date = d
    entries = [Entry(date(2026, 9, 20)), Entry(B1)]
    out = regimes.regime_segments(entries, key=lambda e: e.target_date, bounds=(B1,))
    assert [len(seg) for _, seg in out] == [1, 1]


def test_unsorted_or_duplicate_boundaries_are_refused(monkeypatch):
    monkeypatch.setattr(config, "REGIME_BOUNDARIES", ("2026-10-05", "2026-09-21"))
    with pytest.raises(ValueError):
        regimes.boundaries()
    monkeypatch.setattr(config, "REGIME_BOUNDARIES", ("2026-09-21", "2026-09-21"))
    with pytest.raises(ValueError):
        regimes.boundaries()
